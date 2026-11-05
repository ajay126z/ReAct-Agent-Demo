"""Command-line interface.

Examples::

    react-mcp tools
    react-mcp call calculator --args '{"expression": "(17 * 3) + 9"}'
    react-mcp ask "What is (17 * 3) + 9, and what time is it in UTC?"
    react-mcp protocol-demo
    react-mcp serve --http
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from mcp import types as mcp_types
from rich.json import JSON
from rich.text import Text

from . import __version__
from .agent.llm import LLMError, OllamaLLM
from .agent.memory import ConversationMemory, ConversationTurn, SessionStore
from .agent.react import ReActAgent
from .config import Settings, load_settings
from .mcp_client.base import MCPClientError, ToolSpec, content_blocks_to_text
from .mcp_client.raw_client import RawMCPClient
from .mcp_client.sdk_client import SDKMCPClient
from .observability import render
from .observability.logging import setup_logging
from .observability.otel import configure_otel, shutdown_otel
from .observability.tracer import Tracer
from .server import run_server

COMMANDS = ("serve", "tools", "call", "ask", "repl", "session", "protocol-demo", "doctor")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", help="Ollama model (default: REACT_MCP_MODEL)")
    parser.add_argument("--ollama-host", help="Ollama base URL")
    parser.add_argument("--max-steps", type=int, help="Maximum ReAct steps")
    parser.add_argument("--temperature", type=float, help="Sampling temperature")
    parser.add_argument("--num-ctx", type=int, help="Ollama context window")
    parser.add_argument(
        "--think",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable thinking output for models that support it",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], help="MCP transport")
    parser.add_argument("--server-url", help="Streamable HTTP MCP endpoint")
    parser.add_argument("--trace-dir", help="Directory for trace artifacts")
    parser.add_argument("--no-trace", action="store_true", help="Disable JSONL trace files")
    parser.add_argument("--progress", action="store_true", help="Show MCP tool progress updates")
    parser.add_argument("--server-logs", action="store_true", help="Show MCP server log messages")
    parser.add_argument(
        "--auto-approve-writes",
        action="store_true",
        help="Allow write_file without an interactive elicitation prompt",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the final answer")
    parser.add_argument("--otel", choices=["none", "console", "otlp"], help="OTel exporter")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log verbosity",
    )
    return parser


def _add_session_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-s",
        "--session",
        metavar="NAME",
        help="Resume or create a named session under data/sessions/",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Resume the most recently updated session",
    )


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="react-mcp",
        description="ReAct agent + MCP client/server with local Ollama models.",
    )
    parser.add_argument("--version", action="version", version=f"react-mcp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", parents=[common], help="Run the MCP server")
    serve.add_argument("--http", action="store_true", help="Serve Streamable HTTP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("tools", parents=[common], help="List MCP tools")

    call = sub.add_parser("call", parents=[common], help="Call one MCP tool directly")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="JSON object of tool arguments")

    ask = sub.add_parser("ask", parents=[common], help="Ask the ReAct agent one question")
    ask.add_argument("question", nargs="+")
    _add_session_args(ask)

    repl = sub.add_parser("repl", parents=[common], help="Interactive agent session")
    _add_session_args(repl)

    session = sub.add_parser("session", parents=[common], help="Manage saved sessions")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("list", parents=[common], help="List saved sessions")
    delete = session_sub.add_parser("delete", parents=[common], help="Delete a saved session")
    delete.add_argument("session_name", metavar="NAME")

    demo = sub.add_parser("protocol-demo", parents=[common], help="Show raw JSON-RPC MCP frames")
    demo.add_argument("--expression", default="(17 * 3) + 9")
    demo.add_argument("--no-echo", action="store_true", help="Do not print frames")

    sub.add_parser("doctor", parents=[common], help="Check Ollama and the MCP server")
    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    return load_settings(
        model=getattr(args, "model", None),
        ollama_host=getattr(args, "ollama_host", None),
        max_steps=getattr(args, "max_steps", None),
        temperature=getattr(args, "temperature", None),
        num_ctx=getattr(args, "num_ctx", None),
        think=getattr(args, "think", None),
        transport=getattr(args, "transport", None),
        server_url=getattr(args, "server_url", None),
        trace_dir=getattr(args, "trace_dir", None),
        otel=getattr(args, "otel", None),
        log_level=getattr(args, "log_level", None),
        require_write_approval=False if getattr(args, "auto_approve_writes", False) else None,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


def _make_tracer(
    settings: Settings,
    *,
    enabled: bool,
    run_name: str,
    metadata: dict[str, Any] | None = None,
    echo_protocol: bool = False,
) -> Tracer | None:
    if not enabled:
        return None
    base = {
        "model": settings.model,
        "transport": settings.transport,
        "server_url": settings.server_url,
        "python": platform.python_version(),
        "mcp_sdk": _package_version("mcp"),
        "cwd": str(Path.cwd()),
    }
    base.update(metadata or {})
    return Tracer(
        settings.trace_dir,
        run_name=run_name,
        metadata=base,
        echo_protocol=echo_protocol,
    )


def _close_tracer(tracer: Tracer | None, metrics: dict[str, Any] | None, *, quiet: bool) -> None:
    if tracer is None:
        return
    tracer.close(metrics or {})
    if not quiet:
        if metrics:
            render.print_metrics(metrics)
        render.console.print(f"[dim]trace:[/dim] {tracer.run_dir}")


def _tracer_label(tracer: Tracer | None, settings: Settings) -> str:
    return str(tracer.run_dir) if tracer is not None else f"{settings.trace_dir} (disabled)"


def _session_context(
    args: argparse.Namespace, settings: Settings
) -> tuple[str | None, SessionStore]:
    store = SessionStore(settings.session_dir)
    name = getattr(args, "session", None)
    if getattr(args, "continue_session", False):
        name = store.latest()
        if name is None:
            raise ValueError("No saved sessions found.")
    if name:
        name = store.validate_name(name)
    return name, store


def _load_memory(store: SessionStore, name: str | None, settings: Settings) -> ConversationMemory:
    if name is None:
        return ConversationMemory(max_turns=settings.memory_turns, max_chars=settings.memory_chars)
    return store.load(name, max_turns=settings.memory_turns, max_chars=settings.memory_chars)


def _record_turn(
    store: SessionStore,
    name: str | None,
    memory: ConversationMemory,
    *,
    question: str,
    answer: str,
    settings: Settings,
) -> ConversationTurn:
    turn = ConversationTurn.create(question, answer, model=settings.model)
    if name is not None:
        store.append(name, turn)
    memory.add(turn)
    return turn


def _configure_readline() -> None:
    try:
        import readline
    except ImportError:
        return
    readline.set_history_length(1000)


async def _progress_callback(progress: float, total: float | None, message: str | None) -> None:
    render.print_progress(progress, total, message)


async def _logging_callback(params: Any) -> None:
    render.print_server_log(
        level=str(getattr(params, "level", "info")),
        logger_name=getattr(params, "logger", None),
        data=getattr(params, "data", None),
    )


async def _elicitation_callback(context: Any, params: Any) -> Any:
    message = str(getattr(params, "message", "Approve this tool action?"))
    render.print_text("Write approval", message)
    try:
        answer = await asyncio.to_thread(input, "Allow write? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return mcp_types.ElicitResult(action="cancel")
    if answer.strip().lower() not in {"y", "yes"}:
        return mcp_types.ElicitResult(action="decline")
    return mcp_types.ElicitResult(action="accept", content={"approve": True})


def _client_callbacks(
    args: argparse.Namespace,
    settings: Settings,
    *,
    allow_elicitation: bool = True,
) -> dict[str, Any]:
    server_logs = getattr(args, "server_logs", False)
    return {
        "logging_callback": _logging_callback if server_logs else None,
        "log_level": "info" if server_logs else None,
        "elicitation_callback": (
            _elicitation_callback if allow_elicitation and settings.require_write_approval else None
        ),
    }


def _progress_handler(args: argparse.Namespace) -> Any:
    return _progress_callback if getattr(args, "progress", False) else None


def _raw_tool_specs(result: dict[str, Any]) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=tool.get("name", ""),
            title=tool.get("title"),
            description=tool.get("description", ""),
            input_schema=tool.get("inputSchema", tool.get("input_schema", {})),
        )
        for tool in result.get("tools", [])
    ]


def _prompt_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for message in result.get("messages", []):
        content = message.get("content")
        if isinstance(content, dict):
            parts.append(content.get("text", ""))
        elif isinstance(content, str):
            parts.append(content)
    return "\n".join(part for part in parts if part).strip()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def _cmd_tools(args: argparse.Namespace, settings: Settings) -> int:
    tracer = _make_tracer(settings, enabled=not args.no_trace, run_name="tools")
    try:
        async with SDKMCPClient(
            settings,
            tracer=tracer,
            **_client_callbacks(args, settings, allow_elicitation=False),
        ) as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            prompts = await client.list_prompts()
            if args.quiet:
                for tool in tools:
                    render.console.print(tool.name)
            else:
                render.print_capabilities(
                    protocol_version=client.protocol_version or "?",
                    server=client.server_name or "?",
                    tools=len(tools),
                    resources=len(resources),
                    prompts=len(prompts),
                )
                render.print_tools(tools)
        return 0
    finally:
        _close_tracer(tracer, {}, quiet=args.quiet)


async def _cmd_call(args: argparse.Namespace, settings: Settings) -> int:
    try:
        arguments = json.loads(args.args)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--args is not valid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError("--args must be a JSON object")

    tracer = _make_tracer(
        settings,
        enabled=not args.no_trace,
        run_name="call",
        metadata={"tool": args.tool, "arguments": arguments},
    )
    exit_code = 0
    try:
        async with SDKMCPClient(
            settings, tracer=tracer, **_client_callbacks(args, settings)
        ) as client:
            result = await client.call_tool(
                args.tool, arguments, progress_callback=_progress_handler(args)
            )
        if result.structured is not None:
            payload = json.dumps(result.structured, ensure_ascii=False, default=str)
            render.console.print(JSON(payload))
        else:
            render.print_result(f"tool: {args.tool}", result.content, is_error=result.is_error)
        exit_code = 1 if result.is_error else 0
        return exit_code
    finally:
        _close_tracer(tracer, {}, quiet=args.quiet)


async def _cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    question = " ".join(args.question).strip()
    session_name, store = _session_context(args, settings)
    memory = _load_memory(store, session_name, settings)
    metadata: dict[str, Any] = {"question": question}
    if session_name is not None:
        metadata["session"] = session_name
    tracer = _make_tracer(
        settings,
        enabled=not args.no_trace,
        run_name="ask",
        metadata=metadata,
    )
    metrics: dict[str, Any] = {}
    try:
        if not args.quiet:
            render.print_banner(
                model=settings.model,
                transport=settings.transport,
                trace_dir=_tracer_label(tracer, settings),
                session=session_name,
            )
        async with SDKMCPClient(
            settings, tracer=tracer, **_client_callbacks(args, settings)
        ) as client:
            tools = await client.list_tools()
            if not tools:
                raise RuntimeError("The MCP server exposed no tools.")
            llm = OllamaLLM(settings, tracer=tracer)
            agent = ReActAgent(
                llm=llm,
                client=client,
                tools=tools,
                settings=settings,
                tracer=tracer,
                show_steps=not args.quiet,
                progress_callback=_progress_handler(args),
            )
            result = await agent.run(question, history=memory.turns)
        metrics = result.metrics.as_dict()
        _record_turn(
            store,
            session_name,
            memory,
            question=question,
            answer=result.answer,
            settings=settings,
        )
        if args.quiet:
            render.console.print(Text(result.answer))
        elif session_name is not None:
            render.print_session_status(
                session=session_name,
                turns=len(memory),
                trace=str(tracer.run_dir) if tracer is not None else None,
            )
        return 0
    finally:
        _close_tracer(tracer, metrics, quiet=args.quiet)


async def _cmd_repl(args: argparse.Namespace, settings: Settings) -> int:
    session_name, store = _session_context(args, settings)
    memory = _load_memory(store, session_name, settings)
    metadata: dict[str, Any] = {"session": session_name} if session_name else {}
    tracer = _make_tracer(settings, enabled=not args.no_trace, run_name="repl", metadata=metadata)
    metrics: dict[str, Any] = {}
    try:
        if not args.quiet:
            render.print_banner(
                model=settings.model,
                transport=settings.transport,
                trace_dir=_tracer_label(tracer, settings),
                session=session_name,
            )
        async with SDKMCPClient(
            settings, tracer=tracer, **_client_callbacks(args, settings)
        ) as client:
            tools = await client.list_tools()
            if not tools:
                raise RuntimeError("The MCP server exposed no tools.")
            llm = OllamaLLM(settings, tracer=tracer)
            agent = ReActAgent(
                llm=llm,
                client=client,
                tools=tools,
                settings=settings,
                tracer=tracer,
                show_steps=not args.quiet,
                progress_callback=_progress_handler(args),
            )
            render.print_tools(tools)
            render.print_protocol_note(
                "Commands: :history  :reset  :metrics  :trace  :model  :tools  "
                ":help  :clear  :exit  (Ctrl-D or Ctrl-C also exits)"
            )
            _configure_readline()
            while True:
                try:
                    question = await asyncio.to_thread(input, "\nyou> ")
                except EOFError:
                    break
                except KeyboardInterrupt:
                    render.console.print()
                    break
                question = question.strip()
                if not question:
                    continue
                lowered = question.lower()
                if lowered in {":exit", ":quit", "exit", "quit"}:
                    break
                if lowered == ":tools":
                    render.print_tools(tools)
                    continue
                if lowered == ":help":
                    render.print_protocol_note(
                        "Type a question, or :history, :reset, :metrics, :trace, "
                        ":model, :tools, :clear, :exit."
                    )
                    continue
                if lowered == ":clear":
                    render.console.clear()
                    continue
                if lowered == ":history":
                    render.print_history(memory.turns)
                    continue
                if lowered == ":reset":
                    archived = store.reset(session_name) if session_name else None
                    memory.clear()
                    if archived is not None:
                        render.print_protocol_note(
                            f"Archived {session_name} to {archived}. Starting fresh."
                        )
                    elif session_name is not None:
                        render.print_protocol_note(
                            f"Session {session_name} had no saved turns. Starting fresh."
                        )
                    else:
                        render.print_protocol_note("Conversation history cleared.")
                    continue
                if lowered == ":metrics":
                    if metrics:
                        render.print_metrics(metrics)
                    else:
                        render.print_protocol_note("No completed run yet.")
                    continue
                if lowered == ":trace":
                    if tracer is not None:
                        render.print_text("trace", str(tracer.run_dir))
                    else:
                        render.print_protocol_note("Tracing is disabled.")
                    continue
                if lowered == ":model":
                    render.print_text("model", settings.model)
                    continue
                result = await agent.run(question, history=memory.turns)
                metrics = result.metrics.as_dict()
                _record_turn(
                    store,
                    session_name,
                    memory,
                    question=question,
                    answer=result.answer,
                    settings=settings,
                )
                if not args.quiet:
                    render.print_session_status(
                        session=session_name,
                        turns=len(memory),
                        trace=str(tracer.run_dir) if tracer is not None else None,
                    )
        return 0
    finally:
        _close_tracer(tracer, metrics, quiet=args.quiet)


async def _cmd_session(args: argparse.Namespace, settings: Settings) -> int:
    store = SessionStore(settings.session_dir)
    if args.session_command == "list":
        sessions = store.list_sessions()
        if args.quiet:
            for session in sessions:
                render.console.print(session.name)
        else:
            render.print_sessions(sessions)
        return 0
    if args.session_command == "delete":
        if not store.delete(args.session_name):
            raise ValueError(f"No session named {args.session_name!r}.")
        if args.quiet:
            render.console.print(args.session_name)
        else:
            render.print_protocol_note(f"Deleted session {args.session_name}.")
        return 0
    raise ValueError(f"Unknown session command: {args.session_command!r}")


async def _cmd_protocol_demo(args: argparse.Namespace, settings: Settings) -> int:
    tracer = _make_tracer(
        settings,
        enabled=not args.no_trace,
        run_name="protocol-demo",
        metadata={"expression": args.expression},
        echo_protocol=not args.no_echo,
    )
    try:
        if not args.quiet:
            render.print_banner(
                model="(no LLM in this demo)",
                transport="stdio (raw JSON-RPC)",
                trace_dir=_tracer_label(tracer, settings),
            )
        async with RawMCPClient(settings, tracer=tracer, echo=not args.no_echo) as client:
            render.print_protocol_note("1. initialize: negotiate protocol and capabilities")
            await client.initialize()

            render.print_protocol_note("2. tools/list: discover the callable tools")
            tools_result = await client.list_tools_raw()
            render.print_protocol_note("   resources/list and prompts/list")
            resources_result = await client.list_resources_raw()
            prompts_result = await client.list_prompts_raw()
            render.print_capabilities(
                protocol_version=client.protocol_version or "?",
                server=str(client.server_info.get("name", "?")),
                tools=len(tools_result.get("tools", [])),
                resources=len(resources_result.get("resources", [])),
                prompts=len(prompts_result.get("prompts", [])),
            )
            render.print_tools(_raw_tool_specs(tools_result))

            render.print_protocol_note("3. tools/call: invoke calculator")
            call_result = await client.call_tool_raw("calculator", {"expression": args.expression})
            text = content_blocks_to_text(list(call_result.get("content", [])))
            is_error = bool(call_result.get("isError", call_result.get("is_error", False)))
            render.print_result("calculator result", text, is_error=is_error)

            render.print_protocol_note("4. resources/read")
            uris = [item.get("uri") for item in resources_result.get("resources", [])]
            render.print_text("resources", "\n".join(str(uri) for uri in uris) or "(none)")
            notes = await client.read_resource_raw("notes://index")
            note_text = content_blocks_to_text(list(notes.get("contents", [])))
            render.print_text("notes://index", note_text[:1500] or "(empty)")

            render.print_protocol_note("5. prompts/get")
            names = [item.get("name") for item in prompts_result.get("prompts", [])]
            render.print_text("prompts", "\n".join(str(name) for name in names) or "(none)")
            prompt = await client.get_prompt_raw("explain_mcp")
            render.print_text("explain_mcp prompt", _prompt_text(prompt) or "(empty)")

            render.print_protocol_note(
                "Raw frames were written to protocol.jsonl in the trace directory."
            )
        return 0
    finally:
        _close_tracer(tracer, {}, quiet=args.quiet)


async def _cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    all_ok = True
    render.print_doctor_row("python", True, platform.python_version())
    render.print_doctor_row("mcp sdk", True, _package_version("mcp"))
    render.print_doctor_row("ollama pkg", True, _package_version("ollama"))

    llm = OllamaLLM(settings)
    try:
        models = await llm.list_models()
        render.print_doctor_row("ollama", True, f"{len(models)} model(s) at {settings.ollama_host}")
        model_ok = settings.model in models or any(
            candidate.split(":")[0] == settings.model.split(":")[0] for candidate in models
        )
        detail = settings.model + (" is installed" if model_ok else " is NOT in `ollama list`")
        render.print_doctor_row("model", model_ok, detail)
        all_ok = all_ok and model_ok
    except LLMError as exc:
        render.print_doctor_row("ollama", False, str(exc))
        all_ok = False

    try:
        protocol = "?"
        async with SDKMCPClient(settings) as client:
            tools = await client.list_tools()
            protocol = client.protocol_version or "?"
        render.print_doctor_row(
            "mcp server", True, f"{len(tools)} tool(s), protocol {protocol}, {settings.transport}"
        )
    except Exception as exc:
        render.print_doctor_row("mcp server", False, f"{type(exc).__name__}: {exc}")
        all_ok = False

    return 0 if all_ok else 1


async def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "tools":
        return await _cmd_tools(args, settings)
    if args.command == "call":
        return await _cmd_call(args, settings)
    if args.command == "ask":
        return await _cmd_ask(args, settings)
    if args.command == "repl":
        return await _cmd_repl(args, settings)
    if args.command == "session":
        return await _cmd_session(args, settings)
    if args.command == "protocol-demo":
        return await _cmd_protocol_demo(args, settings)
    if args.command == "doctor":
        return await _cmd_doctor(args, settings)
    raise ValueError(f"Unknown command: {args.command!r}")


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = _settings_from_args(args)
    if args.quiet and args.log_level is None:
        settings = settings.model_copy(update={"log_level": "WARNING"})
    setup_logging(settings.log_level, quiet=args.quiet)

    try:
        if args.command == "serve":
            transport = "http" if args.http else settings.transport
            run_server(
                transport=transport,
                host=args.host,
                port=args.port,
                settings=settings,
            )
            return 0

        if settings.otel != "none":
            configure_otel(settings.otel, service_name="react-mcp")
        return asyncio.run(_dispatch(args, settings))
    except KeyboardInterrupt:
        render.console.print("\n[dim]interrupted[/dim]")
        return 130
    except (LLMError, MCPClientError, RuntimeError, ValueError) as exc:
        render.print_error(str(exc))
        return 1
    except Exception as exc:  # pragma: no cover - unexpected CLI failure
        render.print_error(f"{type(exc).__name__}: {exc}")
        if settings.log_level == "DEBUG":
            raise
        return 1
    finally:
        shutdown_otel()


if __name__ == "__main__":
    sys.exit(main())
