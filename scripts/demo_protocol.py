#!/usr/bin/env python
"""Print the raw JSON-RPC frames of a complete MCP session.

Run it with::

    uv run python scripts/demo_protocol.py
    uv run python scripts/demo_protocol.py --expression "2 ** 10" --no-echo

Unlike the agent, this script does not call an LLM. It uses the hand-rolled
client in ``react_mcp.mcp_client.raw_client`` so every frame is visible and is
also written to ``traces/<run>/protocol.jsonl``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from react_mcp.config import load_settings  # noqa: E402
from react_mcp.mcp_client.base import content_blocks_to_text  # noqa: E402
from react_mcp.mcp_client.raw_client import RawMCPClient  # noqa: E402
from react_mcp.observability import render  # noqa: E402
from react_mcp.observability.logging import setup_logging  # noqa: E402
from react_mcp.observability.tracer import Tracer  # noqa: E402


def _tool_specs(result: dict) -> list:
    from react_mcp.mcp_client.base import ToolSpec

    return [
        ToolSpec(
            name=tool.get("name", ""),
            title=tool.get("title"),
            description=tool.get("description", ""),
            input_schema=tool.get("inputSchema", tool.get("input_schema", {})),
        )
        for tool in result.get("tools", [])
    ]


async def demo(settings, tracer: Tracer | None, *, echo: bool, expression: str) -> None:
    async with RawMCPClient(settings, tracer=tracer, echo=echo) as client:
        render.print_protocol_note("== initialize ==")
        await client.initialize()

        render.print_protocol_note("== discovery ==")
        tools = await client.list_tools_raw()
        resources = await client.list_resources_raw()
        prompts = await client.list_prompts_raw()
        render.print_capabilities(
            protocol_version=client.protocol_version or "?",
            server=str(client.server_info.get("name", "?")),
            tools=len(tools.get("tools", [])),
            resources=len(resources.get("resources", [])),
            prompts=len(prompts.get("prompts", [])),
        )
        render.print_tools(_tool_specs(tools))

        render.print_protocol_note(f"== tools/call calculator: {expression} ==")
        result = await client.call_tool_raw("calculator", {"expression": expression})
        text = content_blocks_to_text(list(result.get("content", [])))
        render.print_result(
            "calculator",
            text,
            is_error=bool(result.get("isError", result.get("is_error", False))),
        )

        render.print_protocol_note("== resources/read notes://index ==")
        note = await client.read_resource_raw("notes://index")
        render.print_text(
            "notes://index",
            content_blocks_to_text(list(note.get("contents", [])))[:1200] or "(empty)",
        )

        render.print_protocol_note("== prompts/get explain_mcp ==")
        prompt = await client.get_prompt_raw("explain_mcp")
        parts = []
        for message in prompt.get("messages", []):
            content = message.get("content")
            if isinstance(content, dict):
                parts.append(content.get("text", ""))
        render.print_text("explain_mcp", "\n".join(parts).strip() or "(empty)")

        if client.instructions:
            render.print_text("server instructions", client.instructions)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show raw MCP JSON-RPC frames.")
    parser.add_argument("--expression", default="(17 * 3) + 9")
    parser.add_argument("--no-echo", action="store_true", help="Record frames without printing")
    parser.add_argument("--no-trace", action="store_true", help="Do not write trace files")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging("WARNING")
    tracer = None
    if not args.no_trace:
        tracer = Tracer(
            settings.trace_dir,
            run_name="demo-protocol",
            metadata={"expression": args.expression, "mode": "raw-jsonrpc"},
            echo_protocol=not args.no_echo,
        )
    try:
        asyncio.run(demo(settings, tracer, echo=not args.no_echo, expression=args.expression))
    finally:
        if tracer is not None:
            tracer.close({})
            render.console.print(f"[dim]trace:[/dim] {tracer.run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
