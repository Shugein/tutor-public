"""Deterministic deadline delivery for structured real dates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys

from telegram import Bot
from telegram.request import HTTPXRequest

from tutor import history
from tutor.config import Settings
from tutor.pregate import mark_reminded, plan_deadlines
from tutor.tools.telegram import send_markdown


@dataclass(frozen=True, slots=True)
class DeadlineRun:
    handled: bool
    tg_sent: int
    messages: tuple[str, ...]
    reason: str


def _message(description: str, due: datetime, now: datetime) -> str:
    if due <= now:
        return f"Дедлайн уже прошел: {description}. Сделай это сейчас."
    return f"Дедлайн в {due:%H:%M}: {description}. Успеешь?"


async def run_deadlines(
    settings: Settings,
    cwd: Path,
    *,
    dry_run: bool,
    now: datetime,
    log,
) -> DeadlineRun:
    """Return handled=False only for legacy free-form files."""
    plan = plan_deadlines(cwd, now)
    log.write(
        "deadline_plan",
        runtime="deterministic",
        structured=plan.structured,
        action_count=len(plan.actions),
        reason=plan.reason,
    )
    if not plan.structured:
        return DeadlineRun(False, 0, (), plan.reason)
    messages: list[str] = []
    bot: Bot | None = None
    request: HTTPXRequest | None = None
    if plan.actions and not dry_run:
        request = HTTPXRequest(
            connect_timeout=settings.tg_timeout_s,
            read_timeout=settings.tg_timeout_s,
            write_timeout=settings.tg_timeout_s,
            pool_timeout=settings.tg_timeout_s,
        )
        bot = Bot(settings.tg_token.get_secret_value(), request=request)
        await bot.initialize()
    try:
        for action in plan.actions:
            message = _message(action.description, action.due, now)
            if dry_run:
                print(f"[dry-run] Telegram deadline: {message}", file=sys.stderr)
            else:
                assert bot is not None
                await send_markdown(bot, settings.tg_chat_id, message)
                history.append(settings.tutor, "tutor", message)
            # Mark only after the transport returned successfully. A timeout is
            # surfaced and never retried as a whole session.
            mark_reminded(cwd / "deadlines.md", action, now)
            messages.append(message)
            log.write(
                "deadline_sent",
                runtime="deterministic",
                due=action.due.isoformat(),
                dry_run=dry_run,
            )
    finally:
        if bot is not None:
            await bot.shutdown()
        if request is not None:
            await request.shutdown()
    return DeadlineRun(True, len(messages), tuple(messages), plan.reason)
