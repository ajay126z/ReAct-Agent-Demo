#!/usr/bin/env python
"""A no-fluff, single-file ReAct agent with one MCP tool.

Run the agent::

    uv run python docs/minimal_react_agent.py "What is 19 + 23?"

The same file also runs as the MCP stdio server. The agent launches it with
``--server``, discovers the one tool, and runs a text ReAct loop for at most
five attempts before forcing a final answer.

Requires the project's existing ``mcp`` and ``ollama`` packages, plus a running
Ollama server with the chosen model.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, time
import json
import re
import sys
from pathlib import Path
from timeit import timeit
from typing import Any
from datetime import datetime

from mcp import Client, StdioServerParameters
from mcp.server import MCPServer
from mcp.types import Tool
from ollama import AsyncClient

MAX_ATTEMPTS = 5
DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_HOST = "http://localhost:11434"
SCRIPT_PATH = Path(__file__).resolve()

SYSTEM_PROMPT = """\
You are a ReAct agent. Solve the user's task using the tools below.

Available tools:
{tool_catalog}

Reply with exactly one of these formats.

To call a tool:
Thought: <brief reasoning>
Action: <tool name>
Action Input: <one JSON object with the tool arguments>

To finish:
Thought: <why you are done>
Final Answer: <the answer>

Rules:
- Use one action per turn.
- Use only the listed tools.
- Do not write Observation: yourself.
- Prefer the add tool for arithmetic instead of guessing.
"""

RETRY_PROMPT = """\
Your previous response did not match the required format. Reply with either:

Thought: ...
Action: ...
Action Input: {"a": 1, "b": 2}

or:

Thought: ...
Final Answer: ...
"""

FORCE_FINAL_PROMPT = """\
You have reached the retry limit. Do not call any more tools.
Using only the observations above, reply now with:

Final Answer: <your best answer>
"""

FINAL_RE = re.compile(r"final\s*answer\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
ACTION_RE = re.compile(r"^\s*action\s*:\s*([A-Za-z0-9_.-]+)\s*$", re.IGNORECASE | re.MULTILINE)
ACTION_INLINE_RE = re.compile(r"\baction\s*:\s*([A-Za-z0-9_.-]+)", re.IGNORECASE)
INPUT_RE = re.compile(r"action\s*input\s*:\s*", re.IGNORECASE)


# ---------------------------------------------------------------------------
# MCP server: one tool
# ---------------------------------------------------------------------------
mcp = MCPServer("Minimal ReAct Tools")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b

@mcp.tool()
def get_time() -> str:
    """Get current time local timezone."""
    return datetime.strftime(datetime.now(), "%Y-%m-%dT%H-%M-%S")

# ---------------------------------------------------------------------------
# Agent side
# ---------------------------------------------------------------------------
def tool_catalog(tools: list[Tool]) -> str:
    """Render discovered MCP tools into the ReAct system prompt."""
    lines = []
    for tool in tools:
        schema = json.dumps(tool.input_schema or {}, ensure_ascii=False, sort_keys=True)
        lines.append(f"- {tool.name}: {tool.description or ''}\n  Arguments: {schema}")
    return "\n".join(lines)


def parse_response(text: str) -> tuple[str, Any]:
    """Return ('final', answer), ('action', (name, args)), or ('invalid', reason)."""
    final = FINAL_RE.search(text)
    if final:
        return "final", final.group(1).strip()

    action = ACTION_RE.search(text) or ACTION_INLINE_RE.search(text)
    if action is None:
        return "invalid", "missing Action or Final Answer"

    input_match = INPUT_RE.search(text, action.end())
    if input_match is None:
        return "invalid", "missing Action Input"

    raw = text[input_match.end() :].strip()
    start = raw.find("{")
    if start == -1:
        return "invalid", "Action Input must be a JSON object"

    try:
        arguments, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        return "invalid", f"Action Input is not valid JSON: {exc}"

    if not isinstance(arguments, dict):
        return "invalid", "Action Input must be a JSON object"
    return "action", (action.group(1), arguments)


async def call_tool(client: Client, name: str, arguments: dict[str, Any]) -> str:
    """Call one MCP tool and flatten its text content into an observation."""
    try:
        result = await client.call_tool(name, arguments)
    except Exception as exc:  # The SDK raises for unknown tools or transport errors.
        return f"Tool error: {type(exc).__name__}: {exc}"

    text = "\n".join(block.text for block in result.content if getattr(block, "text", None)).strip()
    if result.is_error:
        return f"Tool error: {text or '(no message)'}"
    return text or "(empty result)"


async def chat(llm: AsyncClient, model: str, messages: list[dict[str, str]]) -> str:
    """One non-streaming Ollama call with the ReAct stop sequence."""
    response = await llm.chat(
        model=model,
        messages=messages,
        stream=False,
        think=False,
        keep_alive="10m",
        options={"temperature": 0.1, "stop": ["Observation:"]},
    )
    return (response.message.content or "").strip()


def forced_answer(text: str) -> str:
    """Use the model's Final Answer if present, otherwise its whole reply."""
    final = FINAL_RE.search(text)
    return final.group(1).strip() if final else text.strip() or "(no answer)"


async def run_agent(question: str, *, model: str, host: str) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(SCRIPT_PATH), "--server"],
    )
    llm = AsyncClient(host=host)

    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(tool_catalog=tool_catalog(tools)),
            },
            {"role": "user", "content": question},
        ]

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"\n--- attempt {attempt}/{MAX_ATTEMPTS} ---")
            text = await chat(llm, model, messages)
            print(text)
            messages.append({"role": "assistant", "content": text})

            kind, payload = parse_response(text)
            if kind == "final":
                print(f"\nAnswer: {payload}")
                return

            if kind == "invalid":
                print(f"Invalid response: {payload}")
                messages.append({"role": "user", "content": RETRY_PROMPT})
                continue

            name, arguments = payload
            print(f"Calling tool: {name}({json.dumps(arguments, ensure_ascii=False)})")
            observation = await call_tool(client, name, arguments)
            print(f"Observation: {observation}")
            messages.append({"role": "user", "content": f"Observation: {observation}"})

        print("\nRetry limit reached; forcing a final answer.")
        messages.append({"role": "user", "content": FORCE_FINAL_PROMPT})
        text = await chat(llm, model, messages)
        print(f"\nAnswer: {forced_answer(text)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal single-file ReAct + MCP example.")
    parser.add_argument("question", nargs="?", help="Question for the agent.")
    parser.add_argument("--server", action="store_true", help="Run as the MCP stdio server.")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama model (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Ollama host (default: {DEFAULT_HOST}).",
    )
    args = parser.parse_args()

    if args.server:
        mcp.run()
        return 0

    if not args.question:
        parser.error("a question is required unless --server is used")

    asyncio.run(run_agent(args.question, model=args.model, host=args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
