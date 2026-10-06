"""CLI-вход cron-петель: python -m tutor.loop --loop morning [--tutor english] [--dry-run].

Шаблон петли — это ЗАДАЧА для агента, решения он принимает сам (TECH_SPEC).

Шаблоны ищутся сначала у тьютора (`prompts/loops/<тьютор>/<петля>.md`), потом
среди общих (`prompts/loops/<петля>.md`). Контентные петли предметные — они лежат
у тьютора; deadlines и review одинаковы для всех и живут в общих. Фоллбэк
односторонний: если у english нет своего morning, он не запустится — это лучше,
чем молча выдать урок по ML.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from telegram import Bot
from telegram.request import HTTPXRequest

from tutor import history
from tutor.config import DEFAULT_TUTOR, PROJECT_ROOT, load_settings
from tutor.harness import SessionResult, run_session
from tutor.runtime import record_delivery_span
from tutor.tools.telegram import send_markdown

LOOPS_DIR = PROJECT_ROOT / "prompts" / "loops"
NO_DELIVERY = "NO_DELIVERY"

# Петля по запросу (episode) стартует не по расписанию, а по слову ученика:
# шапка одна на оба входа — команду в боте и ручной прогон с --arg.
REQUEST_HEADER = """\
Запрос ученика — как он его написал:

{request}

"""


def template_path(tutor: str, loop: str) -> Path | None:
    for path in (LOOPS_DIR / tutor / f"{loop}.md", LOOPS_DIR / f"{loop}.md"):
        if path.exists():
            return path
    return None


def compose(tutor: str, loop: str, request: str = "") -> str | None:
    """Промпт петли: шаблон тьютора плюс запрос, если петля запускается по нему.

    None — шаблона нет; вызывающий решает, что сказать (бот — отказом в чат,
    CLI — ошибкой со списком доступных петель).
    """
    template = template_path(tutor, loop)
    if template is None:
        return None
    body = template.read_text(encoding="utf-8")
    return REQUEST_HEADER.format(request=request) + body if request else body


def available_loops(tutor: str) -> list[str]:
    own = {p.stem for p in (LOOPS_DIR / tutor).glob("*.md")}
    shared = {p.stem for p in LOOPS_DIR.glob("*.md")}
    return sorted(own | shared)


async def _run_and_deliver(prompt: str, loop_name: str, settings,
                           dry_run: bool) -> SessionResult:
    """Run a cron loop and guarantee one user-visible delivery.

    Models are asked to use Telegram tools, but a valid final response is a
    transport-neutral fallback. This is essential for Codex, which may choose a
    final response even when the prompt prefers a tool.
    """
    result = await run_session(prompt, loop_name, settings, dry_run=dry_run)
    if result.result and _intentional_no_delivery(result.result):
        result.result = None
        return result
    if not result.result or result.tg_sent:
        return result
    if dry_run:
        print(f"[dry-run] Telegram final fallback: {result.result[:500]}",
              file=sys.stderr)
    else:
        request = HTTPXRequest(
            connect_timeout=settings.tg_timeout_s,
            read_timeout=settings.tg_timeout_s,
            write_timeout=settings.tg_timeout_s,
            pool_timeout=settings.tg_timeout_s,
        )
        try:
            async with Bot(settings.tg_token.get_secret_value(), request=request) as bot:
                await send_markdown(bot, settings.tg_chat_id, result.result)
        except Exception as exc:
            record_delivery_span(
                settings,
                loop_name=loop_name,
                session_id=result.session_id,
                trace_id=result.trace_id,
                span_id=result.span_id,
                text=result.result,
                transport="loop-fallback",
                error=exc,
            )
            raise
        record_delivery_span(
            settings,
            loop_name=loop_name,
            session_id=result.session_id,
            trace_id=result.trace_id,
            span_id=result.span_id,
            text=result.result,
            transport="loop-fallback",
        )
        history.append(settings.tutor, "tutor", result.result)
    result.tg_sent += 1
    return result


def _intentional_no_delivery(text: str) -> bool:
    normalized = " ".join(text.strip().split()).casefold()
    if normalized == NO_DELIVERY.casefold():
        return True
    # Compatibility for already-deployed prompts while sessions learn the
    # explicit sentinel. These phrases describe silence, not user content.
    return any(phrase in normalized for phrase in (
        "ничего не отправляю",
        "ничего отправлять не нужно",
        "повторно ничего не отправляю",
        "no delivery",
        "send nothing",
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Запуск петли тьютора")
    parser.add_argument("--loop", required=True, help="имя шаблона из prompts/loops/")
    parser.add_argument("--tutor", default=DEFAULT_TUTOR, help="чью петлю запускать")
    parser.add_argument("--dry-run", action="store_true",
                        help="фейковые внешние тулы: печать вместо отправки")
    parser.add_argument("--arg", default="",
                        help="запрос петле, которая стартует по слову, а не по "
                             "расписанию (episode: что смотрим)")
    args = parser.parse_args()

    settings = load_settings(args.tutor)
    prompt = compose(settings.tutor, args.loop, args.arg)
    if prompt is None:
        available = ", ".join(available_loops(settings.tutor)) or "нет ни одного"
        raise SystemExit(f"Нет шаблона петли {args.loop!r} для тьютора {settings.tutor!r} "
                         f"(искал в {LOOPS_DIR / settings.tutor} и {LOOPS_DIR}). "
                         f"Доступные петли: {available}")

    res = asyncio.run(_run_and_deliver(
        prompt, args.loop, settings, dry_run=args.dry_run
    ))

    usage = res.usage or {}
    print(f"tutor={settings.tutor} loop={args.loop} session={res.session_id} "
          f"is_error={res.is_error} tg_sent={res.tg_sent} duration_ms={res.duration_ms} "
          f"out_tokens={usage.get('output_tokens')}")
    if res.result:
        print(f"result: {res.result[:500]}")
    sys.exit(1 if res.is_error else 0)


if __name__ == "__main__":
    main()
