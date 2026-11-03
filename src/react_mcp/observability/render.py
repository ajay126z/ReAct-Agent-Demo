"""Human-readable rendering for the CLI and the protocol demo."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


def print_banner(*, model: str, transport: str, trace_dir: str) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    table.add_row("model", model)
    table.add_row("transport", transport)
    table.add_row("traces", trace_dir)
    console.print(Panel(table, title="ReAct + MCP", border_style="cyan"))


def print_question(question: str) -> None:
    console.print(Panel(Text(question), title="Question", border_style="blue"))


def print_step(
    *,
    step: int,
    thought: str | None,
    action: str | None = None,
    arguments: Any = None,
    raw_output: str | None = None,
) -> None:
    body = Text()
    if thought:
        body.append("Thought: ", style="bold yellow")
        body.append(thought.strip() + "\n")
    if action:
        body.append("Action: ", style="bold magenta")
        body.append(action + "\n")
        body.append("Action Input: ", style="bold magenta")
        body.append(json.dumps(arguments, ensure_ascii=False, default=str))
    elif raw_output:
        body.append("Model output (unparsed):\n", style="bold red")
        body.append(raw_output.strip())
    console.print(Panel(body, title=f"Step {step}", border_style="magenta"))


def print_observation(text: str, *, is_error: bool = False) -> None:
    style = "red" if is_error else "green"
    title = "Observation (error)" if is_error else "Observation"
    console.print(Panel(Text(text), title=title, border_style=style))


def print_final(answer: str, *, steps: int) -> None:
    console.print(Panel(Text(answer), title=f"Final Answer ({steps} steps)", border_style="green"))


def print_metrics(metrics: dict[str, int | float]) -> None:
    table = Table(title="Run metrics", border_style="cyan")
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    for key, value in metrics.items():
        table.add_row(key, f"{value}")
    console.print(table)


def print_tools(tools: list[Any]) -> None:
    table = Table(title="MCP tools", border_style="cyan")
    table.add_column("name", style="bold cyan", no_wrap=True)
    table.add_column("description")
    table.add_column("arguments", style="dim")
    for tool in tools:
        schema = tool.input_schema or {}
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        args = ", ".join(
            f"{name}{'*' if name in required else ''}:{spec.get('type', 'any')}"
            for name, spec in properties.items()
        )
        table.add_row(tool.name, tool.description or "", args or "-")
    console.print(table)


def print_capabilities(
    *, protocol_version: str, server: str, tools: int, resources: int, prompts: int
) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    table.add_row("protocol", protocol_version)
    table.add_row("server", server)
    table.add_row("tools", str(tools))
    table.add_row("resources", str(resources))
    table.add_row("prompts", str(prompts))
    console.print(Panel(table, title="MCP session", border_style="cyan"))


def print_protocol_frame(direction: str, message: dict[str, Any]) -> None:
    if direction in {"send", "out", "client->server"}:
        arrow, style = "-->", "bold cyan"
    else:
        arrow, style = "<--", "bold green"

    kind = "notification" if "id" not in message else f"id={message.get('id')}"
    method = message.get("method") or ("result" if "result" in message else "error")
    console.print(Text(f"{arrow} {method} ({kind})", style=style))
    console.print(JSON(json.dumps(message, ensure_ascii=False, default=str)))


def print_protocol_note(text: str) -> None:
    console.print(Text(text, style="dim italic"))


def print_text(title: str, text: str) -> None:
    console.print(Panel(Text(text), title=title, border_style="blue"))


def print_result(title: str, text: str, *, is_error: bool = False) -> None:
    style = "red" if is_error else "green"
    console.print(Panel(Text(text), title=title, border_style=style))


def print_error(message: str) -> None:
    console.print(Panel(Text(message), title="Error", border_style="red"))


def print_doctor_row(name: str, ok: bool, detail: str) -> None:
    mark = Text("OK", style="bold green") if ok else Text("FAIL", style="bold red")
    console.print(Text.assemble(f"{name:<18}", mark, f"  {detail}"))
