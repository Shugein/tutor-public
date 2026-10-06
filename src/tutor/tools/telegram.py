"""Тулы Telegram: send_telegram, send_quiz. Один файл — один внешний сервис.

Ошибки возвращаются строкой "ERROR: ..." — агент сам решает, что делать.
"""

import random
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import telegramify_markdown
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.request import HTTPXRequest
from telegramify_markdown.config import get_runtime_config

from tutor import history
from tutor.config import PROJECT_ROOT, Settings
from tutor.mcp_tools import create_mcp_server, tool
from tutor.tools import pdf

SERVER_NAME = "telegram"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{name}"
              for name in ("send_telegram", "send_quiz", "send_worksheet",
                           "send_audio", "send_book_pages")]

# без эмодзи-префиксов у заголовков (дефолт telegramify — 📌/✏/📚)
_symbol = get_runtime_config().markdown_symbol
for _level in range(1, 5):
    setattr(_symbol, f"heading_level_{_level}", "")

_CHUNK = 3000  # лимит TG 4096, запас на экранирование MarkdownV2


def _split(text: str, limit: int = _CHUNK) -> list[str]:
    """Режет текст по абзацам на куски ≤ limit (абзац длиннее limit — жёстко)."""
    parts: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:limit])
            para = para[limit:]
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit:
            parts.append(current)
            current = para
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


async def send_markdown(bot: Bot, chat_id: int, text: str) -> None:
    """Агент пишет GFM (** и ##), Telegram его не понимает — конвертируем
    в MarkdownV2; если разметка всё же кривая, шлём как есть."""
    for chunk in _split(text):
        try:
            await bot.send_message(chat_id, telegramify_markdown.markdownify(chunk),
                                   parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest:
            await bot.send_message(chat_id, chunk)


def audio_dir(settings: Settings) -> Path:
    return PROJECT_ROOT / settings.tutor_dir / "audio"


def audio_path(settings: Settings, track: str) -> Path | None:
    """«1.13», «1.13.mp3» или «01.13» → файл трека; None, если номер не разобрать.

    Модель пишет номер так, как увидела его в книге, а файлы названы строго
    `track_<CD>.<NN>.mp3` — приводим к этому виду сами, а не требуем точности.
    """
    parts = track.strip().removesuffix(".mp3").removeprefix("track_").split(".")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return None
    cd, num = int(parts[0]), int(parts[1])
    return audio_dir(settings) / f"track_{cd}.{num:02d}.mp3"


def shuffle_options(options: list[str], correct_idx: int) -> tuple[list[str], int]:
    """Перемешать варианты, сохранив указатель на правильный ТЕКСТ."""
    shuffled = list(options)
    correct = shuffled[correct_idx]
    random.shuffle(shuffled)
    return shuffled, shuffled.index(correct)


def _key_lines(key: list[pdf.Answer]) -> str:
    """Ключ ответов агенту: буквы известны только после перемешивания в туле,
    а проверять лист будет другая сессия — значит, ключ надо записать в память."""
    if not key:
        return "вариантов под галочку на листе нет"
    rows = "\n".join(f"{i}. {a.letters} — {a.question}" for i, a in enumerate(key, 1))
    return f"ключ ответов (на листе его нет, сохрани в память):\n{rows}"


def _require_keyed_choices(key: list[pdf.Answer]) -> None:
    """Reject graded choice blocks whose correct option was never marked.

    ``prepare_choices`` intentionally supports unordered checklists, but a lesson
    worksheet cannot distinguish a forgotten ``[x]`` from such a checklist at
    grading time. Fail before writing or sending the PDF; use ordinary write-in
    boxes for a checklist instead.
    """
    unkeyed = [answer.question for answer in key if answer.letters == "—"]
    if not unkeyed:
        return
    preview = "; ".join(unkeyed[:3])
    suffix = " ..." if len(unkeyed) > 3 else ""
    raise ValueError(
        "у вариантов нет отмеченного правильного ответа `[x]`: "
        f"{preview}{suffix}. Пометь правильный вариант и повтори; "
        "для негрейдящегося чек-листа используй обычные строки, не `[ ]`"
    )


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(e: Exception) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "is_error": True}


class TelegramToolset:
    """Фабрика тулов + счётчик отправленного: харнесс по нему понимает,
    ответил ли агент пользователю сам (чтобы bot.py не дублировал)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._token = settings.tg_token.get_secret_value()
        self._request = lambda: HTTPXRequest(
            connect_timeout=settings.tg_timeout_s, read_timeout=settings.tg_timeout_s,
            write_timeout=settings.tg_timeout_s, pool_timeout=settings.tg_timeout_s)
        self._chat_id = settings.tg_chat_id
        self._tutor = settings.tutor  # буфер диалога и реестр опросов — на тьютора
        self.sent = 0

    def server(self):
        toolset = self

        @tool(
            "send_telegram",
            "Отправить пользователю текстовое сообщение в Telegram",
            {"text": Annotated[str, "Текст сообщения (можно Markdown)"]},
        )
        async def send_telegram(args: dict[str, Any]) -> dict[str, Any]:
            text = args["text"]
            try:
                async with Bot(toolset._token, request=toolset._request()) as bot:
                    await send_markdown(bot, toolset._chat_id, text)
            except Exception as e:
                return _err(e)
            toolset.sent += 1
            history.append(toolset._tutor, "tutor", text)
            return _ok("ok")

        @tool(
            "send_quiz",
            "Отправить quiz-опрос в Telegram (микропроверка знания). Порядок "
            "вариантов перемешивается автоматически — варианты должны быть "
            "самодостаточными, без ссылок на позиции («оба выше», «A и B»)",
            {
                "question": Annotated[str, "Вопрос, до 300 символов"],
                "options": Annotated[list[str], "2–10 вариантов, до 100 символов каждый"],
                "correct_idx": Annotated[int, "Индекс правильного варианта, с 0"],
                "explanation": Annotated[str, "Пояснение после ответа, до 200 символов"],
            },
        )
        async def send_quiz(args: dict[str, Any]) -> dict[str, Any]:
            # перемешиваем сами: модель систематически ставит верный ответ первым
            options, correct_idx = shuffle_options(args["options"], args["correct_idx"])
            try:
                async with Bot(toolset._token, request=toolset._request()) as bot:
                    msg = await bot.send_poll(
                        toolset._chat_id,
                        question=args["question"],
                        options=options,
                        type="quiz",
                        correct_option_id=correct_idx,
                        explanation=args["explanation"],
                        is_anonymous=False,
                    )
            except Exception as e:
                return _err(e)
            toolset.sent += 1
            history.append(toolset._tutor, "tutor", f"[quiz] {args['question']}")
            history.register_poll(toolset._tutor, msg.poll.id, args["question"],
                                  options, correct_idx)
            return _ok(f"poll_id={msg.poll.id}")

        @tool(
            "send_worksheet",
            "Собрать урок дня в один PDF (A4, место под рукописный ответ) и "
            "отправить его файлом в Telegram: с iPad открывается в GoodNotes. "
            "Это единственный носитель урока: теория, тесты и письменные "
            "задания идут одним документом, а не отдельными сообщениями. "
            "Разметка: `##` — секция листа, `___` отдельной строкой — линейка "
            "под ответ (ставь столько, сколько нужно места), `- [ ] вариант` "
            "и `- [x] верный` — варианты под галочку. Порядок вариантов "
            "перемешивает тул и возвращает ключ ответов; блок с вариантами "
            "без `[x]` отклоняет до отправки. Сохрани ключ себе: на листе "
            "его нет. Копия листа остаётся в worksheets/ — тул "
            "возвращает её ИМЯ, и ключ надо сохранить под тем же именем "
            "(<имя листа>-key.md): второй лист за день не затирает первый, "
            "а встаёт рядом с суффиксом",
            {
                "title": Annotated[str, "Заголовок листа, он же имя файла"],
                "markdown": Annotated[str, "Урок в markdown: секции, `___`, `- [ ]`"],
                "unit": Annotated[str, "Юнит для имени файла и футера, например unit-01"],
            },
        )
        async def send_worksheet(args: dict[str, Any]) -> dict[str, Any]:
            settings = toolset._settings
            when = datetime.now(ZoneInfo(settings.timezone))
            path = pdf.free_worksheet_path(settings, when, args["unit"])
            try:
                data, key = pdf.render_worksheet(
                    args["markdown"], args["title"],
                    footer=f"{args['unit']} · {when:%Y-%m-%d}")
                _require_keyed_choices(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                async with Bot(toolset._token, request=toolset._request()) as bot:
                    await bot.send_document(toolset._chat_id, document=path,
                                            filename=f"{args['title']}.pdf",
                                            caption=args["title"])
            except Exception as e:
                return _err(e)
            toolset.sent += 1
            history.append(toolset._tutor, "tutor", f"[лист урока] {args['title']}")
            return _ok(f"worksheets/{path.name}\n{_key_lines(key)}")

        @tool(
            "send_audio",
            "Отправить аудио-фрагмент учебника в Telegram по номеру трека — "
            "тому же, которым книга помечает задание («1.13» из «▼1.13» "
            "на странице или «$ 1.13 page 12» в ключах). Аудио без задания "
            "— просто звук: вопросы к треку идут на листе, не в подписи",
            {
                "track": Annotated[str, "Номер трека, например 1.13"],
                "caption": Annotated[str, "Одна строка: что слушать и сколько раз"],
            },
        )
        async def send_audio(args: dict[str, Any]) -> dict[str, Any]:
            path = audio_path(toolset._settings, args["track"])
            if path is None or not path.exists():
                available = audio_dir(toolset._settings)
                return _err(FileNotFoundError(
                    f"нет трека {args['track']}; аудио лежит в {available} "
                    "(если папки нет — треки ещё не импортированы)"))
            try:
                async with Bot(toolset._token, request=toolset._request()) as bot:
                    await bot.send_audio(toolset._chat_id, audio=path,
                                         title=f"Track {args['track']}",
                                         caption=args["caption"])
            except Exception as e:
                return _err(e)
            toolset.sent += 1
            history.append(toolset._tutor, "tutor",
                           f"[аудио {args['track']}] {args['caption']}")
            return _ok("ok")

        @tool(
            "send_book_pages",
            "Отправить страницы учебника ученику отдельным PDF: текст для "
            "чтения, схему или картинку, которые на лист не переписать. "
            "Источник — 'sb' (Student's Book) или 'wb' (Workbook); ключи "
            "отправить нельзя. Номера страниц — как у Read по этому же файлу "
            f"юнита, не печатные номера книги; не больше {pdf.MAX_BOOK_PAGES} "
            "за раз. Задания к тексту всё равно идут на листе: страница без "
            "заданий — это чтение без проверки",
            {
                "source": Annotated[str, "'sb' или 'wb'"],
                "unit": Annotated[str, "Юнит, например unit-01"],
                "pages": Annotated[str, "Страницы в файле юнита: «2», «1-3», «1,4»"],
                "caption": Annotated[str, "Одна строка: что это и что с этим делать"],
            },
        )
        async def send_book_pages(args: dict[str, Any]) -> dict[str, Any]:
            settings = toolset._settings
            source = args["source"].strip().lower()
            if source not in pdf.BOOK_SOURCES:
                return _err(ValueError(
                    f"источник «{args['source']}» недоступен; можно "
                    f"{' или '.join(pdf.BOOK_SOURCES)} — ключи ученику не отправляют"))
            path = pdf.book_pdf_path(settings, source, args["unit"])
            if not path.exists():
                return _err(FileNotFoundError(
                    f"нет файла {path.relative_to(PROJECT_ROOT)} — проверь юнит"))
            try:
                data, pages = pdf.extract_pages(path, args["pages"])
                name = f"{source}-{args['unit']}-p{'-'.join(map(str, pages))}.pdf"
                async with Bot(toolset._token, request=toolset._request()) as bot:
                    await bot.send_document(toolset._chat_id, document=data,
                                            filename=name, caption=args["caption"])
            except Exception as e:
                return _err(e)
            toolset.sent += 1
            history.append(toolset._tutor, "tutor",
                           f"[{pdf.BOOK_SOURCES[source]} {args['unit']} "
                           f"стр. {', '.join(map(str, pages))}] {args['caption']}")
            return _ok(f"отправлено страниц: {len(pages)} ({name})")

        return create_mcp_server(
            SERVER_NAME,
            tools=[send_telegram, send_quiz, send_worksheet, send_audio,
                   send_book_pages])
