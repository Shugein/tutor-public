"""Conservative deterministic gates that may skip an unnecessary model call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class PregateDecision:
    skip_model: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DeadlineAction:
    due: datetime
    description: str
    raw_block: str


@dataclass(frozen=True, slots=True)
class DeadlinePlan:
    structured: bool
    actions: tuple[DeadlineAction, ...]
    reason: str


_REAL_DATES = re.compile(
    r"^## Реальные даты\s*$\n(?P<body>.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_ISO_DATE = re.compile(
    r"(?P<day>20\d{2}-\d{2}-\d{2})(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2}))?"
)
_BULLET = re.compile(r"^- .*?(?=^- |\Z)", re.MULTILINE | re.DOTALL)


def plan_deadlines(cwd: Path, now: datetime) -> DeadlinePlan:
    path = cwd / "deadlines.md"
    if not path.exists():
        return DeadlinePlan(True, (), "deadlines.md absent")
    text = path.read_text(encoding="utf-8")
    section = _REAL_DATES.search(text)
    if section is None:
        return DeadlinePlan(False, (), "unstructured deadlines: model fallback")
    if 23 <= now.hour or now.hour < 8:
        return DeadlinePlan(True, (), "night quiet hours")

    actions: list[DeadlineAction] = []
    for bullet in _BULLET.finditer(section.group("body")):
        block = bullet.group(0).rstrip()
        match = _ISO_DATE.search(block)
        if match is None:
            continue
        if re.search(rf"reminded\s+{now:%Y-%m-%d}", block, re.IGNORECASE):
            continue
        if re.search(
            r"(?:status\s*:\s*)?(?:submitted|completed|готово|завершено)",
            block,
            re.IGNORECASE,
        ):
            continue
        day = datetime.strptime(match.group("day"), "%Y-%m-%d").date()
        due_time = (
            time(23, 59)
            if match.group("hour") is None
            else time(int(match.group("hour")), int(match.group("minute")))
        )
        due = datetime.combine(day, due_time, tzinfo=now.tzinfo)
        if due > now and (due - now).total_seconds() > 3 * 3600:
            continue
        first_line = block.splitlines()[0]
        description = first_line[match.end():].lstrip(" -:—").strip().rstrip(".")
        description = re.split(
            r"\s+(?:Не отправлено|Not submitted|Статус:)",
            description,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip().rstrip(".")
        if description:
            actions.append(DeadlineAction(due, description, block))
    reason = "deadline actions ready" if actions else "no due structured real dates"
    return DeadlinePlan(True, tuple(actions), reason)


def deadline_pregate(cwd: Path, now: datetime) -> PregateDecision:
    """Skip only when a structured real-date section proves there is no work.

    Free-form or ambiguous files always fall back to the model. This makes the
    optimization quality-preserving while old product state is normalized.
    """
    plan = plan_deadlines(cwd, now)
    if not plan.structured:
        return PregateDecision(False, plan.reason)
    if plan.actions:
        return PregateDecision(False, f"deadline needs delivery: {plan.actions[0].due.isoformat()}")
    return PregateDecision(True, plan.reason)


def mark_reminded(path: Path, action: DeadlineAction, now: datetime) -> None:
    """Atomically add the reminder marker after a confirmed send."""
    text = path.read_text(encoding="utf-8")
    if action.raw_block not in text:
        raise RuntimeError("deadline block changed before reminder mark")
    marked = action.raw_block + f"\n  - Reminded {now:%Y-%m-%d %H:%M %Z}"
    updated = text.replace(action.raw_block, marked, 1)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
