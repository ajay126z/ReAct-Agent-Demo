"""MCP server entrypoint.

Run over stdio (the default, used by the client subprocess)::

    python -m react_mcp.server

Or over Streamable HTTP for inspection with ``mcp dev`` / a browser client::

    python -m react_mcp.server --http --port 8000
"""

from __future__ import annotations

import argparse
import sys

from mcp.server import MCPServer

from . import __version__
from .config import Settings
from .observability.logging import get_logger, setup_logging
from .observability.otel import configure_otel, shutdown_otel
from .tools import Toolbox, register_prompts, register_resources, register_tools

log = get_logger("server")

INSTRUCTIONS = """\
Local learning server for a ReAct agent. Use the calculator and time tools for
deterministic answers, search_notes/read_note to consult the bundled notes, and
the file tools to inspect the sandboxed workspace. Every file path is relative
to that workspace; absolute paths and path traversal are rejected.
"""


def create_server(settings: Settings | None = None) -> MCPServer:
    """Build a fully registered MCP server (also used by in-memory tests)."""
    settings = settings or Settings()
    settings.ensure_dirs()

    server = MCPServer(
        "ReAct Tools",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level,
    )
    toolbox = Toolbox(settings)
    register_tools(server, toolbox)
    register_resources(server, toolbox, settings)
    register_prompts(server)
    return server


# Module-level instance so `mcp dev src/react_mcp/server.py` and `mcp run` work.
mcp = create_server()


def run_server(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    settings: Settings | None = None,
) -> None:
    settings = settings or Settings()
    setup_logging(settings.log_level)
    if settings.otel != "none":
        configure_otel(settings.otel, service_name="react-mcp-server")

    server = create_server(settings)
    try:
        if transport == "stdio":
            server.run()
        elif transport in {"http", "streamable-http"}:
            server.run(transport="streamable-http", host=host, port=port)
        else:
            raise ValueError(f"Unknown transport: {transport!r}")
    finally:
        shutdown_otel()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the ReAct MCP server.")
    parser.add_argument("--http", action="store_true", help="Use Streamable HTTP instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    run_server(
        transport="http" if args.http else "stdio",
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
