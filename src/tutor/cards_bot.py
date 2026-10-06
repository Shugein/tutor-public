"""Карточный бот: ревью SRS инлайн-кнопками. Один на проект, без LLM.

Почему отдельный бот, а не тьютор: десятки тапов в день не могут быть
SDK-сессиями, а поток карточек засорил бы диалог с тьютором. Здесь нет ни
модели, ни промптов — только очередь из SQLite и кнопки FSRS.

Очередь двухслойная (решение 2026-08-14): сперва долг — карточки, которым пора,
— затем урок: новые слова показываются с открытым ответом и одной кнопкой,
и только после этого попадают в расписание. Слово, которое спрашивают раньше,
чем показали, не проверяется, а угадывается.

Запуск: python -m tutor.cards_bot (токен TG_TOKEN_CARDS).
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from tutor import srs, tts
from tutor.bot import HeartbeatRequest, heartbeat_file, watchdog
from tutor.config import load_settings

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tutor.cards")

SHOW = "show"    # показать оборот
HINT = "hint"    # подсказка, не раскрывая ответа
RATE = "rate"    # оценить и перейти к следующей
LEARN = "learn"  # урок пройден: карточка уходит в расписание

# Подписи как в Anki: не изобретаем словарь там, где у пользователя уже есть привычка
BUTTONS = [("again", "Again"), ("hard", "Hard"), ("good", "Good"), ("easy", "Easy")]

# Вопрос, который карточка задаёт. Тип виден на лице: у recognition и cloze
# лицо выглядит одинаково — строка текста, — но спрашивают они разное, и без
# подписи «uk» на пропуск отвечают переводом, а на слово вписывают форму.
PROMPTS = {
    "recognition": "What does it mean?",
    "cloze": "Fill the gap",
    "production": "How do you say it?",
}


def _escape(text: str) -> str:
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text


def _counter(due_left: int, new_left: int) -> str:
    parts = [f"Left: {due_left}"] if due_left else []
    if new_left:
        parts.append(f"new: {new_left}")
    return " · ".join(parts) or "Left: 0"


def _lesson_view(card: srs.Row, left: int) -> tuple[str, InlineKeyboardMarkup]:
    """Урок: первое знакомство со словом. Ответ открыт — спрашивать нечего.

    Это и есть починка первого прогона: слово, которого он не видел, сперва
    показывается целиком — значение, пример, откуда взялось, — и только потом
    начинает спрашиваться.
    """
    text = f"🆕 *{_escape(card.front)}*\n\n{_escape(card.back)}"
    if card.example:
        text += f"\n\n_{_escape(tts.strip_ipa(card.example))}_"
    if card.context:
        text += f"\n\n{_escape(card.context)}"
    text += f"\n\nTo learn: {left}"
    keyboard = [[InlineKeyboardButton("Got it", callback_data=f"{LEARN}:{card.id}")]]
    return text, InlineKeyboardMarkup(keyboard)


def _front_view(card: srs.Row, due_left: int, new_left: int = 0,
                hint: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    text = f"*{_escape(card.front)}*"
    prompt = PROMPTS.get(card.kind)
    if prompt:
        text += f"\n\n{_escape(prompt)}"
    if hint and card.hint:
        text += f"\n\n💡 _{_escape(card.hint)}_"
    text += f"\n\n{_escape(_counter(due_left, new_left))}"
    row = [InlineKeyboardButton("Show answer", callback_data=f"{SHOW}:{card.id}")]
    if card.hint and not hint:  # подсказка по кнопке: кто помнит, тот её не увидит
        row.insert(0, InlineKeyboardButton("Hint", callback_data=f"{HINT}:{card.id}"))
    return text, InlineKeyboardMarkup([row])


def _back_view(card: srs.Row) -> tuple[str, InlineKeyboardMarkup]:
    text = f"*{_escape(card.front)}*\n\n{_escape(card.back)}"
    if card.example:
        text += f"\n\n_{_escape(tts.strip_ipa(card.example))}_"
    if card.context:
        text += f"\n\n{_escape(card.context)}"
    keyboard = [[InlineKeyboardButton(label, callback_data=f"{RATE}:{card.id}:{rating}")
                 for rating, label in BUTTONS]]
    return text, InlineKeyboardMarkup(keyboard)


def _next_card(per_day: int, tutor: str | None = None):
    """Следующая карточка: сначала долг, потом урок.

    Долг вперёд — он копится и портит расписание, а новые слова подождут до
    завтра без потерь. Возвращает (карточка, урок ли это, сколько due, сколько новых).
    """
    with srs.session() as conn:
        due = srs.due_cards(conn, tutor, limit=200)
        new = srs.new_cards(conn, tutor, per_day=per_day)
    if due:
        return due[0], False, len(due), len(new)
    if new:
        return new[0], True, 0, len(new)
    return None, False, 0, 0


async def _send_voice(chat, card: srs.Row) -> None:
    """Голосовое с примером — отдельным сообщением под карточкой.

    Только там, где ответ уже открыт: на уроке и на обороте. На лицевой стороне
    звук был бы подсказкой — услышать пример значит услышать ответ.
    """
    path = tts.card_audio(card.id)
    if path is None:
        return
    try:
        with path.open("rb") as audio:
            await chat.send_voice(audio)
    except Exception as e:  # молчащая карточка лучше оборванного повторения
        log.warning("не отправилось голосовое карточки %d: %s", card.id, e)


async def _send_next(chat, per_day: int, edit=None) -> None:
    """Показать следующую карточку очереди — новым сообщением или правкой старого."""
    card, lesson, due_left, new_left = _next_card(per_day)
    if card is None:
        done = "All done for now 👌"
        await (edit.edit_text(done) if edit else chat.send_message(done))
        return
    text, keyboard = (_lesson_view(card, new_left) if lesson
                      else _front_view(card, due_left, new_left))
    if edit:
        await edit.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await chat.send_message(text, reply_markup=keyboard,
                                parse_mode=ParseMode.MARKDOWN_V2)
    if lesson:
        await _send_voice(chat, card)


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_next(update.effective_chat, context.bot_data["new_per_day"])


async def _on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with srs.session() as conn:
        overall = srs.stats(conn)
        by_tutor = {t: srs.stats(conn, t)
                    for t in context.bot_data["tutors"]}
    lines = [f"Cards: {overall['total']} total, {overall['due']} due, "
             f"{overall['reviewed_today']} reviewed today, "
             f"{overall['learned_today']} learned today"]
    lines += [f"— {tutor}: {d['total']} total, {d['due']} due, "
              f"{d['waiting']} waiting to be learned"
              for tutor, d in by_tutor.items() if d["total"]]
    await update.effective_chat.send_message("\n".join(lines))


async def _on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != context.bot_data["chat_id"]:  # whitelist и на кнопках
        return
    await query.answer()  # снять «часики» с кнопки
    action, *rest = query.data.split(":")
    per_day = context.bot_data["new_per_day"]

    if action in (SHOW, HINT):
        with srs.session() as conn:
            card = srs.get_card(conn, int(rest[0]))
            left = len(srs.due_cards(conn, limit=200)) if action == HINT else 0
        if card is None:
            await query.edit_message_text("That card is gone — /start for the next one")
            return
        text, keyboard = (_front_view(card, left, hint=True) if action == HINT
                          else _back_view(card))
        await query.edit_message_text(text, reply_markup=keyboard,
                                      parse_mode=ParseMode.MARKDOWN_V2)
        if action == SHOW:  # ответ открыт — теперь можно и озвучить
            await _send_voice(update.effective_chat, card)
        return

    if action == LEARN:
        with srs.session() as conn:
            srs.introduce(conn, int(rest[0]))
        await _send_next(update.effective_chat, per_day, edit=query.message)
        return

    if action == RATE:
        card_id, rating = int(rest[0]), rest[1]
        try:
            with srs.session() as conn:
                srs.review(conn, card_id, rating)
        except ValueError as e:  # карточку удалили между показом и оценкой
            log.warning("оценка не применилась: %s", e)
        await _send_next(update.effective_chat, per_day, edit=query.message)


def main() -> None:
    settings = load_settings()  # общий .env; тьютор здесь не при чём
    if settings.tg_token_cards is None:
        raise SystemExit("В .env нет TG_TOKEN_CARDS — токена карточного бота. "
                         "Образец — .env.example")
    with srs.session() as conn:  # создать базу и схему при первом запуске
        log.info("карточек в базе: %d", srs.stats(conn)["total"])

    # пульс и сторож те же, что у тьюторских ботов: молча умерший карточный
    # бот — это молча прекратившееся повторение, healthcheck обязан заметить
    beat_file = heartbeat_file("cards")
    beat_file.parent.mkdir(exist_ok=True)

    async def post_init(app: Application) -> None:
        app.bot_data["watchdog_task"] = asyncio.create_task(watchdog(beat_file))

    app = (Application.builder()
           .token(settings.tg_token_cards.get_secret_value())
           # пул размера 1 — дефолт PTB для getUpdates, повторяем его
           .get_updates_request(HeartbeatRequest(
               beat_file, connection_pool_size=1,
               connect_timeout=settings.tg_timeout_s, read_timeout=settings.tg_timeout_s))
           # дефолтные 5 с не переживают прокси (см. tg_timeout_s в config.py)
           .connect_timeout(settings.tg_timeout_s)
           .read_timeout(settings.tg_timeout_s)
           .write_timeout(settings.tg_timeout_s)
           .pool_timeout(settings.tg_timeout_s)
           .post_init(post_init)
           .build())
    app.bot_data["tutors"] = list(settings.tutors)
    app.bot_data["chat_id"] = settings.tg_chat_id
    app.bot_data["new_per_day"] = settings.cards_new_per_day
    whitelist = filters.Chat(settings.tg_chat_id)
    app.add_handler(CommandHandler("start", _on_start, filters=whitelist))
    app.add_handler(CommandHandler("stats", _on_stats, filters=whitelist))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & whitelist, _on_start))
    app.add_handler(CallbackQueryHandler(_on_button))
    log.info("карточный бот запущен, whitelist chat_id=%d", settings.tg_chat_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
