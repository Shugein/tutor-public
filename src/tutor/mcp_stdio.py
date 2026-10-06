"""Expose Tutor's provider-neutral tools as standard MCP stdio servers."""

import argparse

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from tutor.config import Settings, load_settings
from tutor.mcp_tools import create_mcp_server
from tutor.tools import calendar as gcalendar
from tutor.tools import docs, forms, sheets
from tutor.tools import srs as srs_tools
from tutor.tools import transcript as transcript_tools
from tutor.tools.fake import make_fake_servers
from tutor.tools.telegram import TelegramToolset


def _real_servers(settings: Settings) -> dict[str, object]:
    return {
        "telegram": TelegramToolset(settings).server(),
        "google": create_mcp_server("google", tools=[
            *forms.make_read_tools(settings),
            *docs.make_tools(settings),
            *sheets.make_tools(settings),
            *gcalendar.make_tools(settings),
        ]),
        "srs": srs_tools.make_server(settings),
        "transcript": transcript_tools.make_server(),
    }


def _server_instance(name: str, tutor: str, dry_run: bool):
    if name == "transcript" and not dry_run:
        return transcript_tools.make_server()
    servers = make_fake_servers() if dry_run else _real_servers(load_settings(tutor))
    try:
        spec = servers[name]
    except KeyError as exc:
        raise SystemExit(f"Unknown MCP server {name!r}") from exc
    if not isinstance(spec, Server):
        raise SystemExit(f"MCP server {name!r} has an invalid implementation")
    return spec


async def _serve(name: str, tutor: str, dry_run: bool) -> None:
    server = _server_instance(name, tutor, dry_run)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--tutor", default="interview")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    anyio.run(_serve, args.server, args.tutor, args.dry_run)


if __name__ == "__main__":
    main()
