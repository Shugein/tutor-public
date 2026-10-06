"""Codex model-runtime boundary.

The product harness owns prompts, tutor selection and JSONL logs. A runtime owns
only one stateless model session and normalizes its result for bot.py/loop.py.
"""

import asyncio
import atexit
import contextlib
import json
import re
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from openai_codex.generated.v2_all import GetAccountRateLimitsResponse
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
    set_span_in_context,
)

from tutor.config import PROJECT_ROOT, Settings
from tutor.tools import srs as srs_tools
from tutor.tools import transcript as transcript_tools
from tutor.tools.telegram import TOOL_NAMES as TG_TOOL_NAMES

BUILTIN_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
GOOGLE_TOOL_NAMES = [
    f"mcp__google__{name}"
    for name in [
        "read_form_responses",
        "create_gdoc",
        "read_gdoc",
        "append_gdoc",
        "update_gdoc",
        "update_gradebook",
        "create_calendar_event",
        "list_calendar_events",
        "update_calendar_event",
        "delete_calendar_event",
    ]
]
MCP_TOOL_NAMES = {
    "telegram": TG_TOOL_NAMES,
    "google": GOOGLE_TOOL_NAMES,
    "srs": srs_tools.TOOL_NAMES,
    "transcript": transcript_tools.TOOL_NAMES,
}


class EventLog(Protocol):
    def write(self, event: str, **fields: Any) -> None: ...


@dataclass(slots=True)
class RuntimeRequest:
    settings: Settings
    loop_name: str
    prompt: str
    system_prompt: str
    cwd: Path
    dry_run: bool
    log: EventLog
    context_manifest: list[dict[str, object]] | None = None
    output_schema: dict[str, Any] | None = None
    disable_mcp: bool = False
    mcp_names: list[str] | None = None
    sandbox: str | None = None
    structured_instruction: str | None = None
    trace_input: str | None = None
    trace_log_path: str | None = None


@dataclass(slots=True)
class RuntimeResult:
    result: str | None
    session_id: str | None
    tg_sent: int
    is_error: bool
    usage: dict[str, Any] | None
    duration_ms: int | None
    trace_id: int | None = None
    span_id: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetObservation:
    total_tokens: int
    delta_tokens: int
    model_steps: int
    over_target: bool
    runaway: bool


class CodexBudgetGuard:
    """Observe targets separately from optional runaway cancellation."""

    def __init__(self, target: int, ceiling: int, step_ceiling: int):
        self.target = target
        self.ceiling = ceiling
        self.step_ceiling = step_ceiling
        self._last_total = 0
        self._steps = 0

    def observe(self, total_tokens: int) -> BudgetObservation:
        delta = max(0, total_tokens - self._last_total)
        if delta:
            self._steps += 1
        self._last_total = max(self._last_total, total_tokens)
        return BudgetObservation(
            total_tokens=total_tokens,
            delta_tokens=delta,
            model_steps=self._steps,
            over_target=total_tokens > self.target,
            runaway=(
                total_tokens > self.ceiling
                or self._steps > self.step_ceiling
            ),
        )


class CodexRunawayError(RuntimeError):
    pass


_TRACE_SECRET = re.compile(
    r"(?i)\b(token|api[_-]?key|password|secret|cookie|authorization)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def _trace_preview(value: Any, limit: int = 4_000) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str
    )
    text = _TRACE_SECRET.sub(r"\1\2[REDACTED]", text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[trace preview truncated, {len(text)} chars total]"


class AgentRuntime(Protocol):
    name: str

    async def run(self, request: RuntimeRequest) -> RuntimeResult: ...


def tools_for(settings: Settings, loop_name: str) -> tuple[list[str], list[str]]:
    """Return Codex builtins and logical MCP servers allowed for this loop."""
    builtin = settings.loop_tools.get(loop_name, BUILTIN_TOOLS)
    servers = settings.loop_mcp.get(loop_name, settings.mcp_servers)
    return builtin, [
        name for name in servers
        if name in MCP_TOOL_NAMES and name in settings.mcp_servers
    ]


async def _with_idle_timeout(
    stream: AsyncIterator[Any],
    timeout_s: float,
    log: EventLog,
) -> AsyncIterator[Any]:
    """Yield stream items while no inter-event silence exceeds timeout_s."""
    agen = stream.__aiter__()
    try:
        while True:
            try:
                yield await asyncio.wait_for(agen.__anext__(), timeout_s)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                log.write("idle_timeout", timeout_s=timeout_s)
                raise TimeoutError(
                    f"сессия молчит {timeout_s:.0f} с — обрываю"
                ) from None
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()


_codex_tracer: Any | None = None


def _setup_codex_tracing(settings: Settings) -> Any | None:
    """Send Codex spans to the existing Phoenix OTLP endpoint."""
    global _codex_tracer
    if _codex_tracer is not None or not settings.otlp_endpoint:
        return _codex_tracer
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({
        "service.name": "tutor",
        "tutor": settings.tutor,
        "runtime": "codex",
    }))
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True)
    ))
    atexit.register(provider.shutdown)
    _codex_tracer = provider.get_tracer("tutor.codex_runtime")
    return _codex_tracer


def _codex_sandbox(value: str) -> Sandbox:
    return {
        "read-only": Sandbox.read_only,
        "workspace-write": Sandbox.workspace_write,
        "full-access": Sandbox.full_access,
    }[value]


def _codex_mcp_config(request: RuntimeRequest, mcp_names: list[str]) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name in mcp_names:
        enabled = [
            full.removeprefix(f"mcp__{name}__")
            for full in MCP_TOOL_NAMES[name]
        ]
        args = [
            "-m",
            "tutor.mcp_stdio",
            "--server",
            name,
            "--tutor",
            request.settings.tutor,
        ]
        if request.dry_run:
            args.append("--dry-run")
        servers[name] = {
            "command": sys.executable,
            "args": args,
            "cwd": str(PROJECT_ROOT),
            "required": True,
            "enabled_tools": enabled,
            "default_tools_approval_mode": "auto",
            "startup_timeout_sec": 20,
            "tool_timeout_sec": 240,
        }
    return {"mcp_servers": servers}


def _codex_developer_instructions(request: RuntimeRequest, builtin: list[str]) -> str:
    capabilities = ", ".join(builtin)
    delivery_instruction = (
        "A scheduled morning loop must produce one user-visible morning message "
        "unless the product profile explicitly marks today as a rest day. Existing "
        "or unfinished work is not a reason for silence: greet the learner, identify the "
        "current assignment and its exact location, give the minimum next action, "
        "and include the brief action-focused reproach required by the loop prompt. "
        "Do not create or resend an artifact merely to satisfy delivery, and do not "
        "return NO_DELIVERY on a study day."
        if request.loop_name == "morning"
        else (
            "If the correct product decision is intentional silence, do not explain "
            "it: return exactly NO_DELIVERY."
        )
    )
    instructions = (
        "Execute one stateless private tutor session. The current directory is "
        "the selected tutor's durable product memory. Read it before deciding, "
        "and update it only when the user-visible task requires continuity. "
        "Use configured MCP tools for external actions. Never claim an external "
        "action succeeded unless its tool completed successfully. Avoid duplicate "
        "messages and artifacts. Treat the final response as fallback delivery "
        "when no send_telegram text message was used; native files, audio and quizzes "
        "do not replace that final text. Use the bounded product context in the "
        "request before reading files. "
        "Only reopen a source marked truncated/missing or an exact artifact needed "
        "for the task. Batch necessary reads and state changes instead of exploring "
        f"the memory tree. {delivery_instruction} Available product capabilities for this "
        f"loop: {capabilities}. Do not inspect or discuss developer configuration, "
        "repository history, agent personas, or unrelated paths."
    )
    if request.structured_instruction:
        instructions += "\n\n" + request.structured_instruction
    return instructions


def _token_total(usage: dict[str, Any]) -> int:
    total = usage.get("total") or {}
    return int(total.get("totalTokens") or total.get("total_tokens") or 0)


def _item_summary(data: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: data[key]
        for key in (
            "id", "type", "status", "server", "tool", "command", "exitCode"
        )
        if key in data
    }
    for key in ("aggregatedOutput", "output", "result"):
        if key in data:
            summary[f"{key}Chars"] = len(str(data[key]))
    if "changes" in data:
        summary["changeCount"] = len(data["changes"] or [])
        summary["changedFiles"] = [
            change.get("path")
            for change in data["changes"] or []
            if isinstance(change, dict) and change.get("path")
        ]
    return summary


def _context_summary(manifest: list[dict[str, object]] | None) -> dict[str, Any]:
    parts = manifest or []
    return {
        "files": len(parts),
        "chars": sum(int(part.get("included_chars") or 0) for part in parts),
        "truncated": sum(bool(part.get("truncated")) for part in parts),
        "paths": [str(part.get("path")) for part in parts if part.get("path")],
    }


def _item_span_name(item_type: str, data: dict[str, Any]) -> str:
    if item_type == "commandExecution":
        command = str(data.get("command") or "command").splitlines()[0]
        verb = "context lookup" if re.search(
            r"\b(grep|rg|sed|tail|head|find|cat)\b", command
        ) else "command"
        return f"{verb} · {_trace_preview(command, 100)}"
    if item_type == "fileChange":
        changes = data.get("changes") or []
        return f"state update · {len(changes)} file{'s' if len(changes) != 1 else ''}"
    if item_type == "mcpToolCall":
        return f"external · {data.get('server', 'mcp')}.{data.get('tool', 'tool')}"
    return f"Codex · {item_type}"


def _record_item_span(tracer: Any, item_type: str, data: dict[str, Any]) -> None:
    """Record only operator-relevant completed items with useful names/data."""
    if item_type not in {"commandExecution", "fileChange", "mcpToolCall"}:
        return
    end_ns = time.time_ns()
    duration_ms = int(data.get("durationMs") or 0)
    span = tracer.start_span(
        _item_span_name(item_type, data),
        kind=SpanKind.INTERNAL,
        start_time=end_ns - duration_ms * 1_000_000,
        attributes={
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.TOOL.value,
            "tutor.item.type": item_type,
        },
    )
    try:
        status = str(data.get("status") or "")
        if item_type == "commandExecution":
            span.set_attribute(SpanAttributes.INPUT_VALUE, _trace_preview(data.get("command")))
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")
            span.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                _trace_preview(data.get("aggregatedOutput"), 2_000),
            )
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
            span.set_attribute("tool.exit_code", int(data.get("exitCode") or 0))
            failed = data.get("exitCode") not in (None, 0)
        elif item_type == "fileChange":
            changes = data.get("changes") or []
            paths = [
                str(change.get("path"))
                for change in changes
                if isinstance(change, dict) and change.get("path")
            ]
            span.set_attribute(SpanAttributes.INPUT_VALUE, ", ".join(paths))
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")
            span.set_attribute("tutor.state.changed_files", paths)
            span.set_attribute("tutor.state.change_count", len(paths))
            failed = "failed" in status.casefold()
        else:
            span.set_attribute(
                SpanAttributes.INPUT_VALUE,
                _trace_preview(data.get("arguments"), 2_000),
            )
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            span.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                _trace_preview(data.get("result"), 2_000),
            )
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
            span.set_attribute("tool.server", str(data.get("server") or ""))
            span.set_attribute("tool.name", str(data.get("tool") or ""))
            failed = "failed" in status.casefold() or bool(data.get("error"))
        span.set_status(Status(StatusCode.ERROR if failed else StatusCode.OK))
    finally:
        span.end(end_time=end_ns)


def _external_side_effect(server: str | None, tool: str | None) -> bool:
    tool = tool or ""
    if server == "telegram":
        return tool.startswith("send_")
    if server == "google":
        return tool.startswith(("create_", "append_", "update_", "delete_"))
    if server == "srs":
        return tool == "add_card"
    return False


def record_delivery_span(
    settings: Settings,
    *,
    loop_name: str,
    session_id: str | None,
    trace_id: int | None,
    span_id: int | None,
    text: str,
    transport: str,
    error: Exception | None = None,
) -> None:
    """Attach caller-side Telegram delivery to the completed runtime trace."""
    if trace_id is None or span_id is None:
        return
    tracer = _setup_codex_tracing(settings)
    if tracer is None:
        return
    parent = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    context = set_span_in_context(NonRecordingSpan(parent))
    with tracer.start_as_current_span(
        f"Telegram delivery · {'ERROR' if error else 'OK'}",
        context=context,
        kind=SpanKind.INTERNAL,
        attributes={
            SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.TOOL.value,
            SpanAttributes.INPUT_VALUE: _trace_preview(text, 8_000),
            SpanAttributes.INPUT_MIME_TYPE: "text/plain",
            "session.id": session_id or "",
            "tutor.loop": loop_name,
            "tutor.delivery.transport": transport,
            "tutor.delivery.delivered": error is None,
        },
    ) as span:
        if error is None:
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, "sent")
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
            span.set_status(Status(StatusCode.OK))
        else:
            span.record_exception(error)
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, _trace_preview(str(error)))
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
            span.set_status(Status(StatusCode.ERROR, str(error)))


class CodexRuntime:
    name = "codex"

    @staticmethod
    async def _rate_limits(codex: AsyncCodex) -> dict[str, Any] | None:
        # Python SDK 0.147 exposes the typed JSON-RPC client but not this
        # App Server endpoint on the public facade yet.
        try:
            response = await codex._client.request(
                "account/rateLimits/read",
                None,
                response_model=GetAccountRateLimitsResponse,
            )
        except Exception:
            return None
        return response.model_dump(by_alias=True, exclude_none=True)

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        settings = request.settings
        builtin, mcp_names = tools_for(settings, request.loop_name)
        if request.mcp_names is not None:
            mcp_names = [name for name in mcp_names if name in request.mcp_names]
        if request.disable_mcp:
            mcp_names = []
        model = settings.codex_model_for(request.loop_name)
        env: dict[str, str] = {}
        if settings.codex_home is not None:
            home = settings.codex_home.expanduser().resolve()
            if not home.is_dir():
                raise RuntimeError(
                    f"CODEX_HOME для product runtime не существует: {home}"
                )
            env["CODEX_HOME"] = str(home)

        config = CodexConfig(
            cwd=str(PROJECT_ROOT),
            env=env or None,
            client_name="tutor_runtime",
            client_title="Tutor Runtime",
            client_version="0.1.0",
        )
        sandbox = _codex_sandbox(request.sandbox or settings.codex_sandbox)
        usage: dict[str, Any] | None = None
        final_text: str | None = None
        duration_ms: int | None = None
        is_error = True
        tg_sent = 0
        side_effect_count = 0
        session_id: str | None = None
        trace_id: int | None = None
        trace_span_id: int | None = None
        target = settings.codex_loop_optimization_targets.get(request.loop_name, 150_000)
        ceiling = settings.codex_loop_runaway_ceilings.get(request.loop_name, target * 2)
        step_ceiling = settings.codex_loop_step_ceilings.get(request.loop_name, 8)
        guard = CodexBudgetGuard(target, ceiling, step_ceiling)
        last_observation = BudgetObservation(0, 0, 0, False, False)
        tracer = _setup_codex_tracing(settings)
        effort = settings.codex_loop_effort.get(request.loop_name) or "default"
        context_summary = _context_summary(request.context_manifest)
        metadata = {
            "context": context_summary,
            "jsonl": request.trace_log_path,
            "dry_run": request.dry_run,
        }
        span_cm = (
            tracer.start_as_current_span(
                f"{settings.tutor}.{request.loop_name} · {model}/{effort}",
                kind=SpanKind.INTERNAL,
                attributes={
                    SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
                    SpanAttributes.INPUT_VALUE: _trace_preview(
                        request.trace_input or request.prompt, 8_000
                    ),
                    SpanAttributes.INPUT_MIME_TYPE: "text/plain",
                    SpanAttributes.LLM_MODEL_NAME: model,
                    SpanAttributes.METADATA: json.dumps(metadata, ensure_ascii=False),
                    "tutor.name": settings.tutor,
                    "tutor.loop": request.loop_name,
                    "gen_ai.request.model": model,
                    "tutor.reasoning_effort": effort,
                    "tutor.dry_run": request.dry_run,
                    "tutor.prompt_chars": len(request.prompt),
                    "tutor.context.files": context_summary["files"],
                    "tutor.context.chars": context_summary["chars"],
                    "tutor.context.truncated_files": context_summary["truncated"],
                    "tutor.context.paths": context_summary["paths"],
                    "tutor.jsonl_path": request.trace_log_path or "",
                },
            )
            if tracer is not None
            else contextlib.nullcontext(None)
        )

        with span_cm as session_span:
            if session_span is not None:
                span_context = session_span.get_span_context()
                if span_context.is_valid:
                    trace_id = span_context.trace_id
                    trace_span_id = span_context.span_id
            async with AsyncCodex(config) as codex:
                rate_limits = await self._rate_limits(codex)
                if rate_limits is not None:
                    request.log.write(
                        "rate_limit_state",
                        runtime=self.name,
                        rate_limits=rate_limits,
                    )
                    if session_span is not None:
                        primary = (
                            rate_limits.get("rateLimits", {}).get("primary", {})
                        )
                        if primary.get("usedPercent") is not None:
                            session_span.set_attribute(
                                "tutor.rate_limit.used_percent",
                                int(primary["usedPercent"]),
                            )
                        if primary.get("resetsAt") is not None:
                            session_span.set_attribute(
                                "tutor.rate_limit.resets_at",
                                int(primary["resetsAt"]),
                            )
                thread = await codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=request.system_prompt,
                    config=_codex_mcp_config(request, mcp_names),
                    cwd=str(request.cwd),
                    developer_instructions=_codex_developer_instructions(request, builtin),
                    ephemeral=True,
                    model=model,
                    sandbox=sandbox,
                    service_name=f"tutor_{settings.tutor}_{request.loop_name}",
                )
                session_id = thread.id
                if session_span is not None:
                    session_span.set_attribute(SpanAttributes.SESSION_ID, session_id)
                request.log.write("init", runtime=self.name, session_id=session_id)
                handle = await thread.turn(
                    request.prompt,
                    effort=settings.codex_loop_effort.get(request.loop_name),
                    output_schema=request.output_schema,
                    sandbox=sandbox,
                )
                stream = _with_idle_timeout(
                    handle.stream(),
                    settings.session_idle_timeout_s,
                    request.log,
                )
                try:
                    async for event in stream:
                        payload = event.payload
                        if event.method == "item/completed":
                            item = getattr(payload.item, "root", payload.item)
                            item_type = getattr(item, "type", type(item).__name__)
                            data = item.model_dump(by_alias=True, exclude_none=True)
                            summary = _item_summary(data)
                            request.log.write(
                                "runtime_item",
                                runtime=self.name,
                                session_id=session_id,
                                item_type=item_type,
                                item=summary,
                            )
                            if tracer is not None:
                                _record_item_span(tracer, item_type, data)
                            if item_type == "agentMessage":
                                final_text = getattr(item, "text", None)
                                request.log.write(
                                    "assistant",
                                    runtime=self.name,
                                    session_id=session_id,
                                    content=[{"type": "text", "text": final_text}],
                                )
                            elif item_type == "mcpToolCall":
                                status = getattr(
                                    getattr(item, "status", None), "value", None
                                )
                                server = getattr(item, "server", None)
                                tool = str(getattr(item, "tool", ""))
                                completed = (
                                    status == "completed"
                                    and getattr(item, "error", None) is None
                                )
                                request.log.write(
                                    "tool_results",
                                    runtime=self.name,
                                    session_id=session_id,
                                    content=[data],
                                )
                                if completed and _external_side_effect(server, tool):
                                    side_effect_count += 1
                                # Native Telegram artifacts are side effects, not a
                                # textual answer. Only send_telegram suppresses the
                                # caller fallback; otherwise a worksheet/book page can
                                # swallow the final explanation (2E check, 2026-08-29).
                                if (
                                    completed
                                    and server == "telegram"
                                    and tool == "send_telegram"
                                ):
                                    tg_sent += 1
                        elif event.method == "thread/tokenUsage/updated":
                            usage = payload.token_usage.model_dump(
                                by_alias=True, exclude_none=True
                            )
                            last_observation = guard.observe(_token_total(usage))
                            request.log.write(
                                "token_usage",
                                runtime=self.name,
                                session_id=session_id,
                                total_tokens=last_observation.total_tokens,
                                delta_tokens=last_observation.delta_tokens,
                                model_steps=last_observation.model_steps,
                                optimization_target=target,
                                runaway_ceiling=ceiling,
                                over_target=last_observation.over_target,
                                runaway=last_observation.runaway,
                                usage=usage,
                            )
                            if session_span is not None:
                                session_span.add_event("token_usage", attributes={
                                    "tutor.tokens.total": last_observation.total_tokens,
                                    "tutor.tokens.delta": last_observation.delta_tokens,
                                    "tutor.model_steps": last_observation.model_steps,
                                    "tutor.tokens.over_target": last_observation.over_target,
                                })
                            if (
                                last_observation.runaway
                                and settings.codex_enforce_runaway_budget
                            ):
                                request.log.write(
                                    "budget_runaway",
                                    runtime=self.name,
                                    session_id=session_id,
                                    side_effect_count=side_effect_count,
                                    total_tokens=last_observation.total_tokens,
                                    model_steps=last_observation.model_steps,
                                )
                                with contextlib.suppress(Exception):
                                    await handle.interrupt()
                                raise CodexRunawayError(
                                    "Codex session exceeded the validated runaway ceiling "
                                    f"({last_observation.total_tokens} tokens, "
                                    f"{last_observation.model_steps} steps, "
                                    f"side effects: {side_effect_count})"
                                )
                        elif event.method == "turn/completed":
                            turn = payload.turn
                            duration_ms = turn.duration_ms
                            status = getattr(turn.status, "value", str(turn.status))
                            is_error = status != "completed" or turn.error is not None
                            if turn.error is not None:
                                request.log.write(
                                    "runtime_error",
                                    runtime=self.name,
                                    session_id=session_id,
                                    error=turn.error.model_dump(
                                        by_alias=True, exclude_none=True
                                    ),
                                )
                except (TimeoutError, CodexRunawayError):
                    with contextlib.suppress(Exception):
                        await handle.interrupt()
                    raise
                finally:
                    with contextlib.suppress(Exception):
                        await stream.aclose()

            request.log.write(
                "budget_summary",
                runtime=self.name,
                session_id=session_id,
                total_tokens=last_observation.total_tokens,
                model_steps=last_observation.model_steps,
                optimization_target=target,
                over_target=last_observation.over_target,
                side_effect_count=side_effect_count,
            )
            if session_span is not None:
                totals = (usage or {}).get("total") or {}
                prompt_tokens = int(
                    totals.get("inputTokens") or totals.get("input_tokens") or 0
                )
                completion_tokens = int(
                    totals.get("outputTokens") or totals.get("output_tokens") or 0
                )
                cached_tokens = int(
                    totals.get("cachedInputTokens")
                    or totals.get("cached_input_tokens")
                    or 0
                )
                session_span.set_attribute(
                    SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_tokens
                )
                session_span.set_attribute(
                    SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_tokens
                )
                session_span.set_attribute(
                    SpanAttributes.LLM_TOKEN_COUNT_TOTAL,
                    last_observation.total_tokens,
                )
                session_span.set_attribute(
                    SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
                    cached_tokens,
                )
                session_span.set_attribute(
                    "tutor.tokens.total", last_observation.total_tokens
                )
                session_span.set_attribute(
                    "tutor.model_steps", last_observation.model_steps
                )
                session_span.set_attribute(
                    "tutor.side_effect_count", side_effect_count
                )
                session_span.set_attribute(
                    "tutor.tokens.over_target", last_observation.over_target
                )
                session_span.set_attribute(
                    "tutor.delivery.mode",
                    "mcp-confirmed"
                    if tg_sent
                    else ("caller-fallback-pending" if final_text else "none"),
                )
                if final_text:
                    session_span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE, _trace_preview(final_text, 12_000)
                    )
                    session_span.set_attribute(
                        SpanAttributes.OUTPUT_MIME_TYPE, "text/plain"
                    )
                session_span.set_status(Status(
                    StatusCode.ERROR if is_error else StatusCode.OK,
                    "Codex turn failed" if is_error else "",
                ))

        return RuntimeResult(
            result=final_text,
            session_id=session_id,
            tg_sent=tg_sent,
            is_error=is_error,
            usage=usage,
            duration_ms=duration_ms,
            trace_id=trace_id,
            span_id=trace_span_id,
        )


def build_runtime() -> AgentRuntime:
    return CodexRuntime()
