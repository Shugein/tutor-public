"""Run the transcript tools as a local MCP stdio server."""

import anyio
from mcp.server.stdio import stdio_server

from tutor_public.transcript import make_server


async def serve() -> None:
    server = make_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    anyio.run(serve)


if __name__ == "__main__":
    main()
