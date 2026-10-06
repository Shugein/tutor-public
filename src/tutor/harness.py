"""Product harness shared by Telegram and cron entry points."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tutor import history
from tutor.config import PROJECT_ROOT, Settings, apply_timezone_override, load_settings
from tutor.context import ContextBundle, build_context
from tutor.deadlines import run_deadlines
from tutor.runtime import (
    BUILTIN_TOOLS,
    MCP_TOOL_NAMES,
    RuntimeRequest,
    _with_idle_timeout,
    build_runtime,
    tools_for as _tools_for,
)
from tutor.structured import (
    CHAT_OUTPUT_SCHEMA,
    CHAT_STRUCTURED_INSTRUCTION,
    WEEKLY_OUTPUT_SCHEMA,
    WEEKLY_STRUCTURED_INSTRUCTION,
    StructuredLoopError,
    apply_chat_output,
    apply_weekly_output,
)

PROMPTS_DIR = PROJECT_ROOT / "prompts"
LOGS_DIR = PROJECT_ROOT / "logs"


@dataclass
class SessionResult:
    result: str | None
    session_id: str | None
    tg_sent: int
    is_error: bool
    usage: dict[str, Any] | None
    duration_ms: int | None
    trace_id: int | None = None
    span_id: int | None = None


def _system_prompt(
    settings: Settings,
    loop_name: str = "",
    *,
    compact_chat: bool = True,
) -> str:
    if loop_name == "chat" and compact_chat:
        paths = [
            PROMPTS_DIR / "chat_system.md",
            PROJECT_ROOT / settings.tutor_dir / "chat_system.md",
        ]
        parts = [
            path.read_text(encoding="utf-8")
            for path in paths
            if path.exists()
        ]
        if parts:
            return "\n\n".join(parts)
    parts = []
    for path in [
        PROMPTS_DIR / "base_system.md",
        PROJECT_ROOT / settings.tutor_dir / "system_prompt.md",
    ]:
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def session_log_path(tutor: str, loop_name: str, tz: str) -> Path:
    date = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    return LOGS_DIR / f"{date}-{tutor}-{loop_name}.jsonl"


class _SessionLog:
    def __init__(self, tutor: str, loop_name: str, tz: str):
        LOGS_DIR.mkdir(exist_ok=True)
        self._path = session_log_path(tutor, loop_name, tz)
        self._tutor = tutor
        self._loop = loop_name

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: str, **fields: Any) -> None:
        line = {
            "ts": datetime.now().astimezone().isoformat(),
            "tutor": self._tutor,
            "loop": self._loop,
            "event": event,
            **fields,
        }
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


_WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]


def _now_line(tz: str, now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo(tz))
    return f"Сейчас {now:%Y-%m-%d %H:%M} ({_WEEKDAYS[now.weekday()]}, {tz})."


def _trace_input(template: str, loop_name: str) -> str:
    """Small operator-facing input; never send the full bounded context to Phoenix."""
    if loop_name == "chat" and "Новое сообщение от ученика в Telegram:" in template:
        body = template.split("Новое сообщение от ученика в Telegram:", 1)[1]
        return body.split("\n\nНедавний диалог", 1)[0].strip()[:8_000]
    return template.strip()[:4_000]


_CHAT_URL = re.compile(r"https?://", re.IGNORECASE)
_CHAT_TELEGRAM_ACTION = re.compile(
    r"(?:\bflash\b|\bquiz\b|\bопрос\w*|\bквиз\w*|\bфлеш\w*|"
    r"(?:5|пять)\s+вопрос)",
    re.IGNORECASE,
)
_CHAT_GOOGLE_ACTION = re.compile(
    r"(?:google\s*(?:doc|docs|calendar|sheet)|gdoc|календар\w*|"
    r"(?:созда|обнов|добав|допиш|перенес|измени|прочит)\w*.{0,32}"
    r"(?:документ|док\b|план\w*|таблиц\w*|calendar|gradebook))",
    re.IGNORECASE,
)
_CHAT_SRS_ACTION = re.compile(
    r"(?:(?:добав|созда|занес|add|create)\w*.{0,32}(?:карточ|srs|word)|"
    r"(?:карточ|srs).{0,32}(?:добав|созда|занес|add|create)\w*)",
    re.IGNORECASE,
)
_CHAT_EXACT_FILE = re.compile(
    r"(?:прочит|открой|посмотри|разбери)\w*.{0,24}(?:файл|pdf|книг|страниц)",
    re.IGNORECASE,
)


def _chat_agent_route(text: str, settings: Settings) -> tuple[bool, list[str]]:
    """Conservatively bypass one-step mode for explicit external/exact work."""
    required: list[str] = []
    telegram_action = bool(_CHAT_TELEGRAM_ACTION.search(text))
    google_action = bool(_CHAT_GOOGLE_ACTION.search(text))
    srs_action = bool(_CHAT_SRS_ACTION.search(text))
    if telegram_action and "telegram" in settings.mcp_servers:
        required.append("telegram")
    if google_action and "google" in settings.mcp_servers:
        required.append("google")
    if srs_action and "srs" in settings.mcp_servers:
        required.append("srs")
    needs_agent = any((
        telegram_action,
        google_action,
        srs_action,
        bool(_CHAT_URL.search(text)),
        bool(_CHAT_EXACT_FILE.search(text)),
    ))
    return needs_agent, required


def _merge_usage(
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Combine two App Server sessions so fallback cost remains visible."""
    if not first:
        return second
    if not second:
        return first
    first_total = first.get("total") or {}
    second_total = second.get("total") or {}
    totals: dict[str, Any] = {}
    for key in set(first_total) | set(second_total):
        left = first_total.get(key)
        right = second_total.get(key)
        if isinstance(left, (int, float)) or isinstance(right, (int, float)):
            totals[key] = (left or 0) + (right or 0)
        else:
            totals[key] = right if right is not None else left
    windows = [
        value for value in (
            first.get("modelContextWindow"), second.get("modelContextWindow")
        ) if isinstance(value, (int, float))
    ]
    merged: dict[str, Any] = {
        "last": second.get("last") or first.get("last"),
        "total": totals,
    }
    if windows:
        merged["modelContextWindow"] = max(windows)
    return merged


def _compose_prompt_data(
    template: str,
    loop_name: str,
    settings: Settings,
    cwd: Path,
    *,
    now: datetime | None = None,
    query: str = "",
) -> tuple[str, ContextBundle]:
    now = now or datetime.now(ZoneInfo(settings.timezone))
    dialog = (
        history.window(settings.tutor, settings.timezone)
        if loop_name != "chat"
        else ""
    )
    bundle = build_context(cwd, loop_name, dialog=dialog, query=query, now=now)
    prompt = (
        f"{_now_line(settings.timezone, now)}\n"
        f"Твоя память - файлы в текущей рабочей директории: {cwd}\n\n"
        f"{template}\n\n"
        f"{bundle.text}"
    )
    if loop_name == "review":
        prompt += f"\n\nЛоги сессий (jsonl): {LOGS_DIR}"
    return prompt, bundle


def _compose_prompt(
    template: str,
    loop_name: str,
    settings: Settings,
    cwd: Path,
) -> str:
    """Compatibility wrapper used by existing prompt tests."""
    return _compose_prompt_data(template, loop_name, settings, cwd)[0]


async def run_session(
    prompt: str,
    loop_name: str,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> SessionResult:
    trace_input = _trace_input(prompt, loop_name)
    settings = apply_timezone_override(settings or load_settings())
    log = _SessionLog(settings.tutor, loop_name, settings.timezone)
    cwd = PROJECT_ROOT / settings.tutor_dir
    now = datetime.now(ZoneInfo(settings.timezone))
    if loop_name == "deadlines":
        deadline_run = await run_deadlines(
            settings, cwd, dry_run=dry_run, now=now, log=log
        )
        if deadline_run.handled:
            usage = {
                "pregate": deadline_run.reason,
                "total": {"totalTokens": 0},
            }
            log.write(
                "result",
                runtime="deterministic",
                session_id=None,
                is_error=False,
                duration_ms=0,
                usage=usage,
                result="\n".join(deadline_run.messages) or None,
            )
            return SessionResult(
                result="\n".join(deadline_run.messages) or None,
                session_id=None,
                tg_sent=deadline_run.tg_sent,
                is_error=False,
                usage=usage,
                duration_ms=0,
            )
    prompt, context = _compose_prompt_data(
        prompt, loop_name, settings, cwd, now=now, query=trace_input
    )
    runtime = build_runtime()
    model = settings.codex_model_for(loop_name)
    effort = settings.codex_loop_effort.get(loop_name)
    structured_weekly = loop_name == "weekly"
    direct_chat_agent = False
    direct_chat_mcp: list[str] = []
    if loop_name == "chat":
        direct_chat_agent, direct_chat_mcp = _chat_agent_route(trace_input, settings)
    structured_chat = (
        loop_name == "chat"
        and not direct_chat_agent
    )
    compact_system_prompt = _system_prompt(settings, loop_name)
    agent_system_prompt = _system_prompt(
        settings,
        loop_name,
        compact_chat=False,
    )
    system_prompt = compact_system_prompt if structured_chat else agent_system_prompt
    execution_mode = (
        "structured-weekly" if structured_weekly
        else "structured-chat" if structured_chat
        else "agent"
    )
    logged_mcp = (
        [] if structured_weekly or structured_chat
        else direct_chat_mcp if direct_chat_agent
        else None
    )
    log.write(
        "session_start",
        runtime=runtime.name,
        model=model,
        effort=effort,
        dry_run=dry_run,
        prompt_chars=len(prompt),
        context_chars=context.included_chars,
        context_budget=context.char_budget,
        context_manifest=context.manifest,
        system_prompt_chars=len(system_prompt),
        execution_mode=execution_mode,
        mcp_names=logged_mcp,
        prompt=prompt,
    )
    result = await runtime.run(RuntimeRequest(
        settings=settings,
        loop_name=loop_name,
        prompt=prompt,
        system_prompt=system_prompt,
        cwd=cwd,
        dry_run=dry_run,
        log=log,
        context_manifest=context.manifest,
        output_schema=(
            WEEKLY_OUTPUT_SCHEMA if structured_weekly
            else CHAT_OUTPUT_SCHEMA if structured_chat
            else None
        ),
        disable_mcp=structured_weekly or structured_chat,
        mcp_names=direct_chat_mcp if direct_chat_agent else None,
        sandbox="read-only" if structured_weekly or structured_chat else None,
        structured_instruction=(
            WEEKLY_STRUCTURED_INSTRUCTION
            if structured_weekly
            else CHAT_STRUCTURED_INSTRUCTION if structured_chat else None
        ),
        trace_input=trace_input,
        trace_log_path=str(log.path),
    ))
    if structured_weekly:
        if result.is_error or not result.result:
            raise RuntimeError("Codex weekly did not return a structured result")
        result.result = apply_weekly_output(cwd, result.result, log)
    elif structured_chat:
        structured_result = result
        fallback_reason: str | None = None
        fallback_mcp: list[str] | None = None
        if structured_result.is_error or not structured_result.result:
            fallback_reason = "structured chat runtime failed"
        else:
            try:
                decision = apply_chat_output(
                    cwd, structured_result.result, log
                )
            except StructuredLoopError as exc:
                fallback_reason = f"invalid structured chat output: {exc}"
            else:
                if decision.needs_agent:
                    fallback_reason = decision.agent_reason or "agent context required"
                    fallback_mcp = list(decision.required_mcp)
                else:
                    result.result = decision.delivery
        if fallback_reason is not None:
            log.write(
                "structured_chat_fallback_start",
                runtime=runtime.name,
                first_session_id=structured_result.session_id,
                reason=fallback_reason,
                mcp_names=fallback_mcp,
            )
            fallback = await runtime.run(RuntimeRequest(
                settings=settings,
                loop_name=loop_name,
                prompt=prompt,
                system_prompt=agent_system_prompt,
                cwd=cwd,
                dry_run=dry_run,
                log=log,
                context_manifest=context.manifest,
                mcp_names=fallback_mcp,
                trace_input=trace_input,
                trace_log_path=str(log.path),
            ))
            fallback.usage = _merge_usage(
                structured_result.usage, fallback.usage
            )
            fallback.duration_ms = (
                (structured_result.duration_ms or 0)
                + (fallback.duration_ms or 0)
            ) or None
            result = fallback
    log.write(
        "result",
        runtime=runtime.name,
        session_id=result.session_id,
        is_error=result.is_error,
        duration_ms=result.duration_ms,
        usage=result.usage,
        result=result.result,
    )
    return SessionResult(
        result=result.result,
        session_id=result.session_id,
        tg_sent=result.tg_sent,
        is_error=result.is_error,
        usage=result.usage,
        duration_ms=result.duration_ms,
        trace_id=result.trace_id,
        span_id=result.span_id,
    )
