"""Structured scheduled-loop decisions applied deterministically by Python."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WEEKLY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["delivery", "edits"],
    "properties": {
        "delivery": {
            "type": "string",
            "description": "Complete Telegram weekly report, ready to send.",
        },
        "edits": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "mode", "find", "content"],
                "properties": {
                    "path": {
                        "type": "string",
                        "enum": ["progress.md", "profile.md", "syllabus.md", "curriculum.md"],
                    },
                    "mode": {"type": "string", "enum": ["append", "replace"]},
                    "find": {
                        "type": ["string", "null"],
                        "description": "Exact unique source text for replace; null for append.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Appended block or exact replacement text.",
                    },
                },
            },
        },
    },
}

WEEKLY_STRUCTURED_INSTRUCTION = """\
## Structured execution contract

Do not call tools, run commands, or reopen files. The bounded context is the
complete evidence for this run. Return only the requested JSON object.

- `delivery`: the complete user-facing weekly Telegram report.
- `edits`: at most six deterministic edits to progress/profile/syllabus or
  curriculum. Use `append` for a new dated block. Use `replace` only with an
  exact, unique `find` string from the bounded context.
- Do not repeat whole files. Keep every edit minimal and preserve all unrelated
  text. Python validates and applies the edits before delivery.
"""


_CHAT_EDIT_PATHS = [
    "progress.md",
    "profile.md",
    "deadlines.md",
    "errors.md",
    "feedback.md",
    "glossary.md",
    "background.md",
    "targets.md",
    "ritual.md",
    "wordbank.md",
]

CHAT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "delivery", "edits", "needs_agent", "agent_reason", "required_mcp",
    ],
    "properties": {
        "delivery": {
            "type": "string",
            "description": "Complete Telegram answer, or empty when needs_agent is true.",
        },
        "edits": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "mode", "find", "content"],
                "properties": {
                    "path": {"type": "string", "enum": _CHAT_EDIT_PATHS},
                    "mode": {"type": "string", "enum": ["append", "replace"]},
                    "find": {"type": ["string", "null"]},
                    "content": {"type": "string"},
                },
            },
        },
        "needs_agent": {"type": "boolean"},
        "agent_reason": {"type": ["string", "null"]},
        "required_mcp": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["telegram", "google", "srs", "transcript"],
            },
        },
    },
}

CHAT_STRUCTURED_INSTRUCTION = """\
## One-step chat contract

Do not call tools, run commands, or reopen files. The bounded context is the
complete evidence for this decision. Return only the requested JSON object.

- `delivery`: a complete user-facing Telegram answer.
- `edits`: at most four minimal deterministic state edits. Advice, explanation,
  and selecting existing material are read-only and must return no edits.
- A real plan/syllabus change needs its external projection kept aligned, so set
  `needs_agent=true` instead of editing curriculum or syllabus here.
- Set `needs_agent=true` when the request requires an external action, web or
  exact artefact access, a native Telegram action, an unavailable fact, a new
  file, or any edit not safely expressible against the bounded context. Then
  return `delivery=""`, `edits=[]`, a short `agent_reason`, and only the MCP
  servers actually required. Web or local exact-file access requires no MCP.
- Otherwise set `needs_agent=false`, `agent_reason=null`, and `required_mcp=[]`.
- If a durable fact explicitly supersedes an existing value visible in the
  bounded context, use `replace` so contradictory current values do not remain.
  Use `append` only for a genuinely new dated fact not already represented.
  Use `replace` only with an exact unique `find` string. Never repeat a whole
  file and never write a record merely saying that you replied.
"""


class StructuredLoopError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatDecision:
    delivery: str
    needs_agent: bool
    agent_reason: str | None
    required_mcp: tuple[str, ...]


def _validated_payload(raw: str, required: set[str], label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredLoopError(f"{label} output is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != required:
        raise StructuredLoopError(f"{label} output has unexpected top-level fields")
    return payload


def _apply_edits(
    cwd: Path,
    edits: Any,
    *,
    allowed: set[str],
    max_items: int,
    max_content: int,
    temp_suffix: str,
) -> list[dict[str, object]]:
    if not isinstance(edits, list) or len(edits) > max_items:
        raise StructuredLoopError(f"edits must be a list of at most {max_items} items")

    updated: dict[Path, str] = {}
    manifest: list[dict[str, object]] = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "mode", "find", "content"}:
            raise StructuredLoopError("structured edit has unexpected fields")
        relative = edit["path"]
        mode = edit["mode"]
        find = edit["find"]
        content = edit["content"]
        if relative not in allowed or mode not in {"append", "replace"}:
            raise StructuredLoopError("structured edit path or mode is not allowed")
        if not isinstance(content, str) or not content.strip() or len(content) > max_content:
            raise StructuredLoopError("structured edit content is empty or too large")
        path = cwd / relative
        if not path.is_file():
            raise StructuredLoopError(f"structured edit source is missing: {relative}")
        source = updated.get(path, path.read_text(encoding="utf-8"))
        if mode == "append":
            if find is not None:
                raise StructuredLoopError("append edit must use find=null")
            result = source.rstrip() + "\n\n" + content.strip() + "\n"
        else:
            if not isinstance(find, str) or not find:
                raise StructuredLoopError("replace edit needs exact non-empty find")
            if source.count(find) != 1:
                raise StructuredLoopError(
                    f"structured replace target must occur exactly once in {relative}"
                )
            result = source.replace(find, content, 1)
        updated[path] = result
        manifest.append({
            "path": relative,
            "mode": mode,
            "before_chars": len(source),
            "after_chars": len(result),
        })

    temporary: list[tuple[Path, Path]] = []
    try:
        for path, text in updated.items():
            tmp = path.with_suffix(path.suffix + temp_suffix)
            tmp.write_text(text, encoding="utf-8")
            temporary.append((path, tmp))
        for path, tmp in temporary:
            tmp.replace(path)
    finally:
        for _, tmp in temporary:
            if tmp.exists():
                tmp.unlink()
    return manifest


def apply_chat_output(cwd: Path, raw: str, log) -> ChatDecision:
    payload = _validated_payload(
        raw,
        {"delivery", "edits", "needs_agent", "agent_reason", "required_mcp"},
        "chat",
    )
    delivery = payload["delivery"]
    edits = payload["edits"]
    needs_agent = payload["needs_agent"]
    reason = payload["agent_reason"]
    required_mcp = payload["required_mcp"]
    if not isinstance(needs_agent, bool):
        raise StructuredLoopError("chat needs_agent must be boolean")
    if not isinstance(required_mcp, list) or any(
        name not in {"telegram", "google", "srs", "transcript"}
        for name in required_mcp
    ):
        raise StructuredLoopError("chat required_mcp is invalid")
    if len(required_mcp) != len(set(required_mcp)):
        raise StructuredLoopError("chat required_mcp contains duplicates")
    if needs_agent:
        if delivery != "" or edits != [] or not isinstance(reason, str) or not reason.strip():
            raise StructuredLoopError("chat escalation must have empty delivery/edits and a reason")
        decision = ChatDecision("", True, reason.strip(), tuple(required_mcp))
        log.write(
            "structured_chat_escalation",
            runtime="deterministic",
            reason=decision.agent_reason,
            required_mcp=list(decision.required_mcp),
        )
        return decision

    if not isinstance(delivery, str) or not delivery.strip() or len(delivery) > 8_000:
        raise StructuredLoopError("chat delivery is empty or too large")
    if reason is not None or required_mcp:
        raise StructuredLoopError("completed structured chat cannot request agent capabilities")
    manifest = _apply_edits(
        cwd,
        edits,
        allowed=set(_CHAT_EDIT_PATHS),
        max_items=4,
        max_content=8_000,
        temp_suffix=".chat.tmp",
    )
    log.write("structured_chat_updates", runtime="deterministic", edits=manifest)
    return ChatDecision(delivery.strip(), False, None, ())


def apply_weekly_output(cwd: Path, raw: str, log) -> str:
    """Validate all edits in memory, then replace canonical files atomically."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredLoopError(f"weekly output is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"delivery", "edits"}:
        raise StructuredLoopError("weekly output has unexpected top-level fields")
    delivery = payload["delivery"]
    edits = payload["edits"]
    if not isinstance(delivery, str) or not delivery.strip() or len(delivery) > 8_000:
        raise StructuredLoopError("weekly delivery is empty or too large")
    if not isinstance(edits, list) or len(edits) > 6:
        raise StructuredLoopError("weekly edits must be a list of at most six items")

    allowed = {"progress.md", "profile.md", "syllabus.md", "curriculum.md"}
    updated: dict[Path, str] = {}
    manifest: list[dict[str, object]] = []
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "mode", "find", "content"}:
            raise StructuredLoopError("weekly edit has unexpected fields")
        relative = edit["path"]
        mode = edit["mode"]
        find = edit["find"]
        content = edit["content"]
        if relative not in allowed or mode not in {"append", "replace"}:
            raise StructuredLoopError("weekly edit path or mode is not allowed")
        if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
            raise StructuredLoopError("weekly edit content is empty or too large")
        path = cwd / relative
        if not path.is_file():
            raise StructuredLoopError(f"weekly edit source is missing: {relative}")
        source = updated.get(path, path.read_text(encoding="utf-8"))
        if mode == "append":
            if find is not None:
                raise StructuredLoopError("append edit must use find=null")
            result = source.rstrip() + "\n\n" + content.strip() + "\n"
        else:
            if not isinstance(find, str) or not find:
                raise StructuredLoopError("replace edit needs exact non-empty find")
            if source.count(find) != 1:
                raise StructuredLoopError(
                    f"weekly replace target must occur exactly once in {relative}"
                )
            result = source.replace(find, content, 1)
        updated[path] = result
        manifest.append({
            "path": relative,
            "mode": mode,
            "before_chars": len(source),
            "after_chars": len(result),
        })

    temporary: list[tuple[Path, Path]] = []
    try:
        for path, text in updated.items():
            tmp = path.with_suffix(path.suffix + ".structured.tmp")
            tmp.write_text(text, encoding="utf-8")
            temporary.append((path, tmp))
        for path, tmp in temporary:
            tmp.replace(path)
    finally:
        for _, tmp in temporary:
            if tmp.exists():
                tmp.unlink()
    log.write("structured_updates", runtime="deterministic", edits=manifest)
    return delivery.strip()
