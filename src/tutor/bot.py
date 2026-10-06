"""Telegram-бот: long polling, входящее сообщение → run_session.

Один процесс — один тьютор — один токен бота: `python -m tutor.bot --tutor english`.
Разводить тьюторов внутри процесса нечем и незачем — polling у PTB на приложение,
а состояние (heartbeat, буфер диалога) и так на тьютора.

Три входа: текст → сессия `chat`; фото или файл (скан, PDF из GoodNotes) →
сессия `check`, которая читает рукопись зрением и разбирает решённое;
`/episode <что смотрю>` → сессия `episode`, список выражений перед просмотром.

Whitelist chat_id — здесь, в коде: авторизация не может зависеть от LLM (ADR-6).
Чужие сообщения молча игнорируются на уровне фильтра.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, PollAnswerHandler, filters)
from telegram.request import HTTPXRequest

from tutor import history
from tutor.config import (DEFAULT_TUTOR, PROJECT_ROOT, Settings,
                          apply_timezone_override, load_settings)
from tutor.harness import run_session
from tutor.runtime import record_delivery_span
from tutor.loop import compose, template_path
from tutor.tools import drive
from tutor.tools.telegram import send_markdown

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tutor.bot")

PROMPT_TEMPLATE = """\
Новое сообщение от ученика в Telegram:

{text}

Недавний диалог (короткая память; долгая — в твоих файлах):
{dialog}

Ответь на сообщение. Твой финальный текст будет отправлен ему в Telegram —
не дублируй его тулом send_telegram. Пиши финальный текст как готовое сообщение
ученику, без служебных фраз о том, что ты собираешься сделать
(«сейчас объясню…», «отправлю сводку…»).
"""

# Фолбэк для тьютора без своего check.md: у interview рукописных листов нет,
# но фото формулы или конспекта он получить может.
CHECK_FALLBACK = """\
Посмотри присланное и ответь по существу: что там, что верно, что нет.
Ошибки разбирай так же, как разбираешь ответы на квизы, и заноси в память.
"""

CHECK_HEADER = """\
ученик прислал файлы в Telegram. Они лежат в твоей рабочей директории:

{files}

Прочитай их сам инструментом Read (это изображения или PDF — ты видишь их
глазами). Написанное от руки читай как есть: ошибка в слове — это ошибка
ученика, а не погрешность распознавания.{caption}

"""



def inbox_dir(settings: Settings) -> Path:
    """Входящие файлы от ученика. Единственное место в памяти тьютора, куда
    пишет код, а не агент: это входной канал, а не память (см. ADR-2)."""
    return PROJECT_ROOT / settings.tutor_dir / "inbox"


def inbox_path(settings: Settings, when: datetime, seq: int, ext: str) -> Path:
    """inbox/<дата>-<время>-<n>.<ext> — имя говорит, когда пришло и каким по счёту."""
    return inbox_dir(settings) / f"{when:%Y-%m-%d-%H%M%S}-{seq}{ext}"


def _extension(message) -> str:
    if message.document and message.document.file_name:
        suffix = Path(message.document.file_name).suffix.lower()
        if suffix:
            return suffix
    if message.document and message.document.mime_type == "application/pdf":
        return ".pdf"
    return ".jpg"  # сжатое фото Telegram всегда jpeg


async def _keep_typing(chat) -> None:
    """Телеграмный «печатает…» живёт ~5 секунд — продлеваем, пока сессия работает."""
    with contextlib.suppress(Exception):  # сетевые сбои индикатора не роняют ответ
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(4)


async def _drop(status) -> None:
    """Убрать плашку. Её может не быть (не ушла) — и её судьба не важнее ответа."""
    if status is not None:
        with contextlib.suppress(Exception):
            await status.delete()


async def _say(context: ContextTypes.DEFAULT_TYPE, update: Update, status, text: str) -> None:
    """Служебное сообщение: правкой плашки, а если её нет — новым сообщением."""
    with contextlib.suppress(Exception):
        if status is not None:
            await status.edit_text(text)
        else:
            await context.bot.send_message(update.effective_chat.id, text)


async def _respond(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   prompt: str, loop_name: str, waiting: str) -> None:
    """Прогнать сессию и отдать её результат в чат. Общий хвост для всех входов."""
    settings = context.bot_data["settings"]
    # Плашка «думаю» — вежливость, а не ответ: её сбой не должен уносить сессию
    # (2026-08-15: reply_text упал с TimedOut через прокси, и вопрос остался без
    # ответа вовсе). Дальше по коду status может быть None.
    status = None
    try:
        status = await update.message.reply_text(waiting)
    except Exception:
        log.warning("плашка ожидания не ушла — отвечаю без неё", exc_info=True)
    typing = asyncio.create_task(_keep_typing(update.effective_chat))

    try:
        res = await run_session(prompt, loop_name, settings)
    except Exception:
        log.exception("сессия упала")
        await _say(context, update, status,
                   "The session crashed. Details are in the server logs.")
        return
    finally:
        typing.cancel()

    log.info("сессия %s (%s): is_error=%s, tg_sent=%d",
             res.session_id, loop_name, res.is_error, res.tg_sent)
    if res.result and not res.tg_sent:
        await _drop(status)
        try:
            await send_markdown(context.bot, update.effective_chat.id, res.result)
        except Exception as exc:
            record_delivery_span(
                settings,
                loop_name=loop_name,
                session_id=res.session_id,
                trace_id=res.trace_id,
                span_id=res.span_id,
                text=res.result,
                transport="bot-fallback",
                error=exc,
            )
            raise
        record_delivery_span(
            settings,
            loop_name=loop_name,
            session_id=res.session_id,
            trace_id=res.trace_id,
            span_id=res.span_id,
            text=res.result,
            transport="bot-fallback",
        )
        history.append(settings.tutor, "tutor", res.result)
    elif not res.tg_sent:
        await _say(context, update, status,
                   "The session finished without an answer — check the logs.")
    else:  # агент уже ответил сам через send_telegram
        await _drop(status)


# Слово-сигнал «решил, забирай»: бэкап GoodNotes уезжает в Drive сам, а когда
# именно — знает только ученик. Поллинг Drive ради двух файлов в день не нужен.
DONE_MARKERS = ("готово", "готов", "сделал", "решил", "порешал", "done")


def _is_done_signal(text: str) -> bool:
    """Первое слово сообщения — сигнал о решённом листе?

    Именно первое слово, а не вхождение: «готов обсудить план» — это диалог,
    а не сигнал, и уводить его в проверку нельзя.
    """
    first = text.strip().lower().lstrip("«\"'").split()
    return bool(first) and first[0].strip(".,!…:;»\"'") in DONE_MARKERS


async def _fetch_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Забрать свежий лист из бэкапа Drive и запустить проверку.

    False — забрать не вышло: причину уже сказали ученику, дальше сообщение
    обрабатывается как обычная реплика.
    """
    settings = context.bot_data["settings"]
    status = await update.message.reply_text("📥 Looking for the latest backup in Drive…")
    tz = apply_timezone_override(settings).timezone
    when = datetime.now(ZoneInfo(tz))
    dest = inbox_path(settings, when, 1, ".pdf")
    # ищем файл свежее последнего забора: старый лист проверять второй раз незачем
    since = context.bot_data.get("last_backup") or (when - timedelta(hours=12))

    try:
        await asyncio.to_thread(drive.fetch_latest_backup, settings.backup_folder,
                                dest, since.astimezone(timezone.utc))
    except drive.BackupNotFound as e:
        await status.edit_text(f"{e}")
        return False
    except Exception as e:  # чаще всего — токен без скоупа drive.readonly
        log.exception("забор бэкапа не удался")
        await status.edit_text(
            f"I could not read Drive: {e}\n\nIf this is about access, the token needs "
            "re-authorising with the drive.readonly scope (scripts/google_auth.py). "
            "Meanwhile you can just send the file here.")
        return False

    context.bot_data["last_backup"] = drive.utcnow()
    await status.delete()
    await _run_check(update, context, [dest], update.message.text)
    return True


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data["settings"]
    text = update.message.text
    log.info("сообщение из TG (%d символов)", len(text))
    dialog = history.recent(settings.tutor) or "(пусто)"  # снимаем ДО записи реплики
    history.append(settings.tutor, "user", text)

    if settings.backup_folder and _is_done_signal(text):
        if await _fetch_backup(update, context):
            return  # проверка уже прошла и ответила

    await _respond(update, context, PROMPT_TEMPLATE.format(text=text, dialog=dialog),
                   "chat", "⏳ Thinking…")


MAX_FILE_MB = 20  # предел getFile у Bot API: больше бот скачать не может
ALBUM_DEBOUNCE = 3.0  # альбом приходит по сообщению на файл — ждём остальные
DOWNLOAD_ATTEMPTS = 3  # прокси рвёт соединение регулярно; повтор дешевле потери листа
DOWNLOAD_BACKOFF_S = 3.0


async def _retry_network(what: str, call):
    """Повторить сетевой вызов к Telegram: обрыв через прокси лечится повтором.

    Повторяем только транспортные сбои. BadRequest по иерархии PTB — тоже
    NetworkError, но это отказ по существу (протухший file_id, слишком большой
    файл), и повторять его бессмысленно.
    """
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return await call()
        except BadRequest:
            raise
        except NetworkError:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            log.warning("%s: попытка %d из %d не удалась, повторяю",
                        what, attempt, DOWNLOAD_ATTEMPTS, exc_info=True)
            await asyncio.sleep(DOWNLOAD_BACKOFF_S * attempt)


async def _download(update: Update, context: ContextTypes.DEFAULT_TYPE, seq: int) -> Path | None:
    """Скачать вложение в inbox тьютора; None — если Telegram его не отдаст."""
    settings = context.bot_data["settings"]
    message = update.message
    media = message.document or (message.photo[-1] if message.photo else None)
    if media is None:
        return None
    if (media.file_size or 0) > MAX_FILE_MB * 1024 * 1024:
        await message.reply_text(
            f"That file is over {MAX_FILE_MB} MB — Telegram will not hand it to a bot. "
            "Send the pages one by one, or export at a smaller size.")
        return None

    tz = apply_timezone_override(settings).timezone
    path = inbox_path(settings, datetime.now(ZoneInfo(tz)), seq, _extension(message))
    path.parent.mkdir(parents=True, exist_ok=True)
    tg_file = await _retry_network("getFile", lambda: context.bot.get_file(media.file_id))
    try:
        await _retry_network("скачивание",
                             lambda: tg_file.download_to_drive(custom_path=path))
    except Exception:
        path.unlink(missing_ok=True)  # обрывок в inbox тьютор прочтёт как настоящий лист
        raise
    log.info("получен файл %s (%d КБ)", path.name, path.stat().st_size // 1024)
    return path


def _check_prompt(settings: Settings, paths: list[Path], caption: str) -> str:
    """Шапка «что пришло» + шаблон проверки тьютора (prompts/loops/<тьютор>/check.md)."""
    template = template_path(settings.tutor, "check")
    body = template.read_text(encoding="utf-8") if template else CHECK_FALLBACK
    files = "\n".join(f"- `inbox/{p.name}`" for p in paths)
    note = f"\n\nПодпись к отправке: «{caption}»" if caption else ""
    return CHECK_HEADER.format(files=files, caption=note) + body


async def _run_check(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     paths: list[Path], caption: str) -> None:
    settings = context.bot_data["settings"]
    names = ", ".join(p.name for p in paths)
    history.append(settings.tutor, "user",
                   f"[прислал {len(paths)} файл(ов): {names}]"
                   + (f" с подписью «{caption}»" if caption else ""))
    # честное ожидание: чтение рукописи и сверка с ключами занимают минуты,
    # а не секунды (замер 2026-08-10: страница из GoodNotes — 11 минут)
    await _respond(update, context, _check_prompt(settings, paths, caption),
                   "check", "👀 Reading and checking — this takes a few minutes…")


def _episode_prompt(settings: Settings, query: str) -> str:
    """Запрос + шаблон prompts/loops/<тьютор>/episode.md — тот же промпт,
    что собирает ручной прогон `--loop episode --arg ...` (см. loop.compose).

    Пустая строка — у тьютора нет такого шаблона: разбор контента предметный,
    и молчаливого фоллбэка тут быть не должно.
    """
    return compose(settings.tutor, "episode", query) or ""


async def _on_episode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/episode Friends S01E01` → список выражений до просмотра.

    Отдельная сессия, а не диалог: у неё свой Terra/medium route и шаблон, а
    механический поиск и отбор транскрипта занимают десятки ходов, которые в
    чате были бы шумом.
    """
    settings = context.bot_data["settings"]
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "What are you going to watch? For example: "
            "`/episode Friends S01E01`", parse_mode="Markdown")
        return

    prompt = _episode_prompt(settings, query)
    if not prompt:
        await update.message.reply_text(
            f"У тьютора {settings.tutor} нет шаблона episode — это петля "
            "английского.")
        return

    log.info("запрос разбора: %s", query)
    history.append(settings.tutor, "user", f"[/episode] {query}")
    await _respond(update, context, prompt, "episode",
                   "🎬 Looking for a transcript…")


# media_group_id → накопленные файлы альбома; живёт секунды, между сообщениями
_albums: dict[str, dict] = {}


def _drop_album(group: str | None) -> None:
    """Страница не доехала — снимаем с проверки весь альбом.

    Проверить лист без одной страницы хуже, чем не проверить вовсе: тьютор
    разберёт неполный ответ как полный и запишет в память несделанное как
    ошибку, а ученик об этом не узнает.
    """
    entry = _albums.pop(group, None) if group else None
    if entry and entry.get("task"):
        entry["task"].cancel()


async def _flush_album(group: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.sleep(ALBUM_DEBOUNCE)
    entry = _albums.pop(group, None)
    if entry and entry["paths"]:
        await _run_check(update, context, entry["paths"], entry["caption"])


async def _on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фото или файл: скачиваем и отдаём сессии проверки.

    Альбом из нескольких страниц Telegram шлёт отдельными сообщениями с общим
    media_group_id — копим их и запускаем ОДНУ сессию на весь лист, иначе
    двухстраничное ДЗ проверялось бы двумя сессиями вслепую друг к другу.
    """
    group = update.message.media_group_id
    seq = len(_albums.get(group, {}).get("paths", [])) + 1 if group else 1
    try:
        path = await _download(update, context, seq)
    except Exception:
        _drop_album(group)
        raise  # дальше — _on_error: сказать вслух, что лист не доехал
    if path is None:
        return
    caption = update.message.caption or ""

    if group is None:
        await _run_check(update, context, [path], caption)
        return

    entry = _albums.setdefault(group, {"paths": [], "caption": ""})
    entry["paths"].append(path)
    entry["caption"] = entry["caption"] or caption
    # таймер отсчитывается от ПОСЛЕДНЕГО пришедшего файла: страницы качаются
    # неравномерно, и фиксированное окно от первой отрезало бы хвост альбома
    if entry.get("task"):
        entry["task"].cancel()
    entry["task"] = asyncio.create_task(_flush_album(group, update, context))


async def _on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на квиз-опрос → в буфер диалога: агент увидит его в следующей сессии."""
    answer = update.poll_answer
    settings = context.bot_data["settings"]
    if answer.user.id != settings.tg_chat_id:  # whitelist и здесь
        return
    poll = history.find_poll(settings.tutor, answer.poll_id)
    if poll is None:
        return
    if not answer.option_ids:  # отозвал голос
        return
    chosen = poll["options"][answer.option_ids[0]]
    verdict = "верно" if answer.option_ids[0] == poll["correct_idx"] else "НЕВЕРНО"
    history.append(settings.tutor, "user",
                   f"[ответ на квиз «{poll['question']}»] выбрал «{chosen}» — {verdict}")
    log.info("ответ на квиз записан в буфер (%s)", verdict)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Последний рубеж: любой сбой в хендлере должен быть слышен в чате.

    2026-08-22: getFile упал с TimedOut через прокси, PTB записал traceback в
    лог — и всё; присланный лист пропал, а ученик узнал об этом на следующий
    день. Тишина — худший вид отказа: о ней невозможно догадаться, и повторить
    отправку в ответ на неё некому.
    """
    log.error("необработанная ошибка в хендлере", exc_info=context.error)
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return
    with contextlib.suppress(Exception):  # сеть уже подводит — молчим честно
        await context.bot.send_message(
            chat.id,
            "Something broke on my side and I did not get that — please send it again.")


def heartbeat_file(tutor: str) -> Path:
    """Пульс бота этого тьютора; его сторожит scripts/healthcheck.py."""
    return PROJECT_ROOT / "logs" / f"bot-{tutor}.heartbeat"


class HeartbeatRequest(HTTPXRequest):
    """Пульс для scripts/healthcheck.py: обновляется на каждом завершившемся
    getUpdates. Именно на запросе, а не в фоновой таске: event loop может жить
    при мёртвом поллинге (зависший пул httpx после обрыва прокси, 2026-07-11).
    """

    def __init__(self, beat_file: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self._beat_file = beat_file

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        result = await super().do_request(*args, **kwargs)
        self._beat_file.touch()
        return result


WATCHDOG_STALE = 3 * 60  # getUpdates завершается каждые ~10 с; 3 мин тишины — транспорт мёртв
WATCHDOG_INTERVAL = 30


async def watchdog(beat_file: Path) -> None:
    """Протух heartbeat → выходим из процесса, restart: always поднимет заново.

    Зависший пул httpx (PTB #4405) изнутри не лечится, поэтому os._exit, а не
    graceful stop — тот сам виснет на прощальном getUpdates. Отсчёт от
    max(mtime, старт процесса) и без touch на старте: если Telegram недоступен
    надолго, файл остаётся честно протухшим и healthcheck дойдёт до алерта.
    """
    started = time.time()
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        last_beat = beat_file.stat().st_mtime if beat_file.exists() else 0.0
        stale = time.time() - max(last_beat, started)
        if stale > WATCHDOG_STALE:
            log.error("heartbeat протух на %d с — поллинг завис, выходим на рестарт", int(stale))
            os._exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот тьютора")
    parser.add_argument("--tutor", default=DEFAULT_TUTOR, help="кого обслуживать")
    args = parser.parse_args()

    settings = load_settings(args.tutor)
    beat_file = heartbeat_file(settings.tutor)
    beat_file.parent.mkdir(exist_ok=True)

    async def post_init(app: Application) -> None:
        # ссылку храним в bot_data, иначе таску съест GC
        app.bot_data["watchdog_task"] = asyncio.create_task(watchdog(beat_file))

    app = (Application.builder()
           .token(settings.tg_token.get_secret_value())
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
    app.bot_data["settings"] = settings
    whitelist = filters.Chat(settings.tg_chat_id)
    app.add_handler(CommandHandler("episode", _on_episode, filters=whitelist))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & whitelist, _on_message))
    # решённый лист: фото с телефона или PDF/картинка из GoodNotes
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.PDF | filters.Document.IMAGE) & whitelist,
        _on_media,
    ))
    app.add_handler(PollAnswerHandler(_on_poll_answer))
    app.add_error_handler(_on_error)
    log.info("бот тьютора %s запущен, whitelist chat_id=%d", settings.tutor, settings.tg_chat_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
