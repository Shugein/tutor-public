"""Тулы транскриптов: fetch_page и fetch_subtitles. Источник текста для петли episode.

Зачем свой тул, когда есть WebFetch: 2026-08-11 первый прогон episode не смог
получить ни одного транскрипта Friends — три источника из шести отказал сам
WebFetch (копирайт), хотя страницы отдаются обычным GET (проверено: HTTP 200,
~4.5k слов). Здесь нет никакого обхода защит — только обычный HTTP-клиент
и yt-dlp для субтитровых дорожек.

Ошибки возвращаются строкой "ERROR: ..." — агент сам решает, что делать.
"""

import asyncio
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any

import httpx

from tutor_public.mcp_tools import create_mcp_server, tool
SERVER_NAME = "transcript"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{name}"
              for name in ("fetch_page", "fetch_subtitles")]

TIMEOUT = 30.0
SUBS_TIMEOUT = 180.0  # yt-dlp ходит в сеть и разбирает плейлист
MAX_BYTES = 5 * 1024 * 1024  # страница транскрипта — сотни КБ; больше не бывает
MAX_CHARS = 300_000  # ~45k слов: эпизод сериала целиком влезает с запасом
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Блочные теги: их границы в тексте — перенос строки. Для транскрипта это
# важнее, чем кажется: реплики разделены <br>/<p>, и без переносов страница
# схлопывается в одну простыню, где не видно, где кончается фраза.
_BLOCKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
           "blockquote", "section", "article", "table"}
_SKIP = {"script", "style", "noscript", "head", "svg"}


class _TextExtractor(HTMLParser):
    """HTML → плоский текст. Namesake библиотек не тянем: одна страница,
    одна задача, а лишняя зависимость в образе живёт вечно."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # пробелы внутри строки схлопываем, пустые строки — не больше двух подряд
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in raw.split("\n")]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


_CUE = re.compile(r"-->")
_TAGS = re.compile(r"<[^>]+>")  # <c>, <00:00:12.345>, <v Speaker>
_INDEX = re.compile(r"^\d+$")


def subtitles_to_text(raw: str) -> str:
    """VTT/SRT → реплики без таймкодов.

    Автосубтитры YouTube «роллятся»: каждая реплика повторяется в следующем
    блоке вместе с новой строкой. Без дедупликации текст раздувается вдвое
    и модель читает одно и то же дважды.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if (not line or _CUE.search(line) or _INDEX.match(line)
                or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:", "STYLE"))):
            continue
        line = _TAGS.sub("", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def _clip(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n[обрезано: было {len(text)} символов]"


def _check_url(url: str) -> str | None:
    if not url.startswith(("http://", "https://")):
        return "ERROR: ссылка должна начинаться с http:// или https://"
    return None


def make_server():
    @tool(
        "fetch_page",
        "Скачать веб-страницу и вернуть её текст без разметки. Для страниц "
        "транскриптов и субтитров, которые не отдаёт WebFetch. Возвращает текст "
        "как есть — проверь сам, что это нужный эпизод, а не пересказ или заглушка",
        {"url": Annotated[str, "Полная ссылка на страницу (http/https)"]},
    )
    async def fetch_page(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        if err := _check_url(url):
            return {"content": [{"type": "text", "text": err}], "is_error": True}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                         headers={"User-Agent": UA}) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.content[:MAX_BYTES]
        except httpx.HTTPStatusError as e:
            return {"content": [{"type": "text", "text":
                                 f"ERROR: сервер ответил {e.response.status_code}"}],
                    "is_error": True}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}],
                    "is_error": True}

        html = body.decode(resp.encoding or "utf-8", errors="replace")
        text = html_to_text(html) if "html" in resp.headers.get("content-type", "") \
            else html
        if not text.strip():
            return {"content": [{"type": "text", "text":
                                 "ERROR: страница пустая — вероятно, текст рисует "
                                 "JavaScript. Это тот же случай, что «не нашёл»"}],
                    "is_error": True}
        words = len(text.split())
        return {"content": [{"type": "text",
                             "text": f"[{url} — {words} слов]\n\n{_clip(text)}"}]}

    @tool(
        "fetch_subtitles",
        "Забрать дорожку субтитров у видео по ссылке (YouTube и другие площадки, "
        "которые умеет yt-dlp) и вернуть текстом без таймкодов. Само видео не "
        "скачивается. Берёт и авторские субтитры, и автоматические",
        {
            "url": Annotated[str, "Ссылка на видео"],
            "lang": Annotated[str, "Код языка дорожки, по умолчанию en"],
        },
    )
    async def fetch_subtitles(args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        lang = (str(args.get("lang") or "en")).strip() or "en"
        if err := _check_url(url):
            return {"content": [{"type": "text", "text": err}], "is_error": True}
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,15}", lang):
            return {"content": [{"type": "text", "text": "ERROR: непонятный код языка"}],
                    "is_error": True}

        with tempfile.TemporaryDirectory() as tmp:
            cmd = ["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs",
                   "--sub-langs", f"{lang},{lang}-orig,{lang}.*", "--sub-format", "vtt",
                   "--no-playlist", "--quiet", "--no-warnings",
                   "-o", str(Path(tmp) / "sub"), "--", url]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=SUBS_TIMEOUT)
            except FileNotFoundError:
                return {"content": [{"type": "text", "text":
                                     "ERROR: yt-dlp не установлен в образе"}], "is_error": True}
            except asyncio.TimeoutError:
                return {"content": [{"type": "text", "text":
                                     f"ERROR: yt-dlp не уложился в {SUBS_TIMEOUT:.0f} с"}],
                        "is_error": True}

            files = sorted(Path(tmp).glob("*.vtt")) or sorted(Path(tmp).glob("*.srt"))
            if not files:
                detail = (stderr.decode(errors="replace").strip().splitlines() or [""])[-1]
                return {"content": [{"type": "text", "text":
                                     f"ERROR: субтитров на «{lang}» у этого видео нет"
                                     + (f" ({detail[:200]})" if detail else "")}],
                        "is_error": True}
            text = subtitles_to_text(files[0].read_text(encoding="utf-8", errors="replace"))

        if not text.strip():
            return {"content": [{"type": "text", "text":
                                 "ERROR: дорожка скачалась пустой"}], "is_error": True}
        words = len(text.split())
        return {"content": [{"type": "text",
                             "text": f"[субтитры {files[0].name} — {words} слов]"
                                     f"\n\n{_clip(text)}"}]}

    return create_mcp_server(SERVER_NAME, tools=[fetch_page, fetch_subtitles])
