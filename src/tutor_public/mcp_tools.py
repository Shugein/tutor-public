"""Provider-neutral MCP tool definitions and low-level stdio servers.

Product handlers use a small local decorator so their schemas do not depend on
any model-provider SDK. Codex launches the resulting ``mcp.server.lowlevel``
servers through ``tutor.mcp_stdio``.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from mcp import types
from mcp.server.lowlevel import Server

ToolResult = dict[str, Any]
ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    description: str | None = None
    if get_origin(annotation) is Annotated:
        annotation, *metadata = get_args(annotation)
        description = next((item for item in metadata if isinstance(item, str)), None)

    origin = get_origin(annotation)
    if annotation is str:
        schema: dict[str, Any] = {"type": "string"}
    elif annotation is int:
        schema = {"type": "integer"}
    elif annotation is float:
        schema = {"type": "number"}
    elif annotation is bool:
        schema = {"type": "boolean"}
    elif origin in (list, Sequence):
        args = get_args(annotation)
        schema = {
            "type": "array",
            "items": _annotation_schema(args[0]) if args else {},
        }
    elif origin in (dict, Mapping) or annotation is dict:
        schema = {"type": "object"}
    else:
        schema = {}

    if description:
        schema["description"] = description
    return schema


def _input_schema(spec: Mapping[str, Any]) -> dict[str, Any]:
    if spec.get("type") == "object":
        return dict(spec)
    properties = {
        name: _annotation_schema(annotation)
        for name, annotation in spec.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def tool(
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
) -> Callable[[ToolHandler], ToolDefinition]:
    """Attach an explicit MCP name, description and JSON input schema."""

    def decorate(handler: ToolHandler) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema=_input_schema(input_schema),
            handler=handler,
        )

    return decorate


def _call_result(result: ToolResult) -> types.CallToolResult:
    content: list[types.TextContent] = []
    for block in result.get("content", []):
        if block.get("type") != "text":
            raise TypeError(f"Unsupported MCP content type: {block.get('type')!r}")
        content.append(types.TextContent(type="text", text=str(block.get("text", ""))))
    return types.CallToolResult(
        content=content,
        isError=bool(result.get("is_error") or result.get("isError")),
    )


def create_mcp_server(name: str, tools: Sequence[ToolDefinition]) -> Server:
    """Build a standard low-level MCP server from explicit tool definitions."""
    definitions = {definition.name: definition for definition in tools}
    if len(definitions) != len(tools):
        raise ValueError(f"Duplicate tool name in MCP server {name!r}")

    server = Server(name)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=definition.name,
                description=definition.description,
                inputSchema=definition.input_schema,
            )
            for definition in definitions.values()
        ]

    @server.call_tool()
    async def call_tool(tool_name: str, arguments: dict[str, Any]):
        try:
            definition = definitions[tool_name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool {tool_name!r} on MCP server {name!r}") from exc
        return _call_result(await definition.handler(arguments))

    return server
