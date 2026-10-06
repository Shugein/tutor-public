"""Фейковые тулы для --dry-run: печать в stdout вместо внешних вызовов.

Имена и серверы зеркалят боевые, чтобы промпты работали без правок.
Файловые тулы агента (Read/Write/Edit) остаются настоящими — dry-run
проверяет рассуждения петли, а не файловую систему.
"""

import json
import sys
from typing import Any

from tutor.mcp_tools import create_mcp_server, tool

_RESULTS = {
    "send_telegram": "ok",
    "send_quiz": "poll_id=dry-poll",
    "send_worksheet": ("worksheets/dry-run.pdf\nключ ответов (на листе его нет, "
                       "сохрани в память):\n1. B — dry-run\n2. A — dry-run"),
    "send_audio": "ok",
    "send_book_pages": "отправлено страниц: 2 (dry-run)",
    "read_form_responses": "[] (dry-run: ответов нет)",
    "create_gdoc": "{'doc_id': 'dry-doc', 'url': 'https://example.com/dry-doc'}",
    "read_gdoc": "# dry-run\n\nСодержимое документа недоступно.",
    "append_gdoc": "ok",
    "update_gdoc": "ok",
    "update_gradebook": "ok",
    "add_card": "card_id=1 (создана)",
    "srs_stats": "карточек всего: 0, к повторению: 0, повторено сегодня: 0",
    "fetch_page": ("[dry-run — 3 слова]\n\nMONICA: dry run.\nROSS: dry run.\n\n"
                   "Это заглушка, а не транскрипт: списка из неё не делают"),
    "fetch_subtitles": ("[dry-run — 3 слова]\n\ndry run subtitles\n\n"
                        "Это заглушка, а не транскрипт: списка из неё не делают"),
    "create_calendar_event": "{'event_id': 'dry-event'}",
    "list_calendar_events": "[] (dry-run: событий нет)",
    "update_calendar_event": "{'event_id': 'dry-event', 'changed': 'start'}",
    "delete_calendar_event": "ok",
}
_TG_NAMES = ["send_telegram", "send_quiz", "send_worksheet", "send_audio",
             "send_book_pages"]
_SRS_NAMES = ["add_card", "srs_stats"]
_TRANSCRIPT_NAMES = ["fetch_page", "fetch_subtitles"]


def _make_tool(name: str, result: str):
    @tool(name, f"[dry-run] {name}: печатает аргументы, наружу не ходит",
          {"type": "object", "additionalProperties": True})
    async def _fake(args: dict[str, Any]) -> dict[str, Any]:
        # STDOUT is the MCP protocol when these tools run as a Codex stdio
        # server. Diagnostics belong on stderr for both runtimes.
        print(f"[dry-run] {name}: {json.dumps(args, ensure_ascii=False)[:300]}",
              file=sys.stderr)
        return {"content": [{"type": "text", "text": result}]}

    return _fake


def make_fake_servers() -> dict[str, Any]:
    """Те же серверы и имена, что в боевом harness — промпты работают без правок."""
    own = set(_TG_NAMES) | set(_SRS_NAMES) | set(_TRANSCRIPT_NAMES)
    return {
        "telegram": create_mcp_server(
            "telegram", tools=[_make_tool(n, _RESULTS[n]) for n in _TG_NAMES]),
        "srs": create_mcp_server(
            "srs", tools=[_make_tool(n, _RESULTS[n]) for n in _SRS_NAMES]),
        "transcript": create_mcp_server(
            "transcript", tools=[_make_tool(n, _RESULTS[n]) for n in _TRANSCRIPT_NAMES]),
        "google": create_mcp_server(
            "google", tools=[_make_tool(n, r) for n, r in _RESULTS.items() if n not in own]),
    }
