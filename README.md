# ReAct + MCP with a local model

A small, runnable system for learning how an agent actually works. It connects
four pieces: a local Ollama model, a text-based ReAct loop, an MCP client, and
an MCP server with deterministic tools. Every model call, tool call, and raw
JSON-RPC frame can be traced on disk.

```text
                 +------------------+
  question ----> |   ReActAgent     | <----> Ollama (qwen3.5:9b)
                 |  Thought/Action  |
                 +---------+--------+
                           |
                     MCPToolClient
                     /            \
             SDKMCPClient     RawMCPClient
                     \            /
                      \          /
                   JSON-RPC over stdio
                           |
                 +---------v--------+
                 |    MCP server    |
                 | calculator, time |
                 | dates, data      |
                 | notes, files     |
                 | resources/prompts|
                 +------------------+
```

The agent uses **text ReAct**, not native tool calling, so the reasoning and
every action are visible in the transcript and trace. The server is local and
sandboxed: no network tools, no arbitrary code execution, and file access is
restricted to `data/workspace`.

## Requirements

- macOS or Linux with Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/) running locally, with at least one model

```bash
ollama list
ollama pull qwen3.5:9b
```

## Quick start

```bash
uv sync --all-extras
cp .env.example .env          # optional
uv run react-mcp doctor
uv run react-mcp tools
uv run react-mcp ask "What is (17 * 3) + 9, and what time is it in UTC?"
```

`ask` prints each Thought, Action, and Observation, then a final answer and run
metrics. Use `--quiet` for just the answer. `write_file` prompts for approval by
default; use `--auto-approve-writes` for non-interactive runs.

## Commands

| Command | What it does |
| --- | --- |
| `react-mcp doctor` | Check Python, Ollama, the model, and the MCP server. |
| `react-mcp tools` | Connect over MCP and list tools, resources, and prompts. |
| `react-mcp call <tool> --args '<json>'` | Call one tool directly, without the LLM. |
| `react-mcp ask "<question>"` | Run the ReAct agent once. |
| `react-mcp repl` | Interactive agent session. |
| `react-mcp repl --session NAME` | Resume or create a saved session. |
| `react-mcp repl --continue` | Resume the most recently updated session. |
| `react-mcp session list` | List saved sessions, turn counts, and update times. |
| `react-mcp session delete NAME` | Delete a saved session. |
| `react-mcp protocol-demo` | Print the raw JSON-RPC frames of a full MCP session. |
| `react-mcp serve` | Run the MCP server over stdio. |
| `react-mcp serve --http --port 8000` | Run it over Streamable HTTP. |

Common flags go after the subcommand:

```bash
uv run react-mcp ask "..." --model llama3.2:latest --max-steps 6
uv run react-mcp ask "..." --no-trace --quiet
uv run react-mcp ask "..." --think                  # models with a thinking channel
uv run react-mcp tools --transport http --server-url http://127.0.0.1:8000/mcp
uv run react-mcp ask "..." --otel console           # requires the otel extra
uv run react-mcp ask "..." --session research       # remember this exchange
uv run react-mcp repl --session research            # continue it interactively
uv run react-mcp ask "..." --progress --server-logs # stream MCP progress and logs
uv run react-mcp ask "..." --auto-approve-writes    # skip write approval prompts
```

Examples worth trying:

```bash
uv run react-mcp ask "Search the notes for observability and summarize what a span is."
uv run react-mcp ask "Read the react note and explain why this agent uses text instead of native tool calling."
uv run react-mcp ask "List the workspace files, then write hello.txt with a short greeting."
uv run react-mcp ask "How many days are between 2026-01-01 and 2026-02-01?"
uv run react-mcp ask "Convert 12 kilometers to miles, then preview scores.csv if it exists."
```

## Sessions

`repl` keeps an in-process conversation by default. Pass `-s/--session NAME` to
resume or create a saved session, or `-c/--continue` to pick the most recently
updated one. `ask` accepts the same flags for one-shot follow-ups.

Sessions live in `data/sessions/NAME.jsonl` and store completed Q/A turns only,
not intermediate ReAct observations. Follow-up prompts receive the last six
turns, capped at 4000 characters total (`REACT_MCP_MEMORY_TURNS` and
`REACT_MCP_MEMORY_CHARS`). REPL commands:

| Command | What it does |
| --- | --- |
| `:history` | Show the current bounded conversation history. |
| `:reset` | Archive the session JSONL and start fresh. |
| `:metrics` | Show metrics from the last completed run. |
| `:trace` | Show the current trace directory. |
| `:model` | Show the active Ollama model. |
| `:tools` | List MCP tools again. |
| `:help` | Show available commands. |
| `:clear` | Clear the terminal. |
| `:exit` | Leave the REPL. |

```bash
uv run react-mcp repl --session research
uv run react-mcp repl --continue
uv run react-mcp session list
uv run react-mcp session delete research
```

## How it works

1. `SDKMCPClient` launches `python -m react_mcp.server` over stdio and performs
   the MCP handshake. It discovers tool names, descriptions, and JSON schemas.
2. `build_system_prompt` renders those schemas into a ReAct prompt. The model is
   told to stop at `Observation:` so it cannot invent tool output.
3. The model replies with a `Thought`, an `Action`, and `Action Input`. The
   parser is forgiving about code fences, prose, and Python-style quotes.
4. The agent calls `tools/call` over MCP, appends the observation to the
   transcript, and loops.
5. A `Final Answer` ends the run. Step limits, parse retries, and a forced-final
   pass keep the loop bounded.

The official SDK client negotiates protocol `2026-07-28` by default, but falls
back to the legacy `initialize` handshake (`2025-11-25`) when write approval
installs an elicitation callback. The educational raw client in
`scripts/demo_protocol.py` always uses the legacy handshake so the frames match
most MCP material.

## MCP surface

Tools:

- `calculator` - arithmetic via an AST whitelist, never `eval`.
- `get_current_time` - IANA timezone, defaults to `UTC`.
- `date_diff` / `add_days` - ISO date and datetime arithmetic.
- `unit_convert` - length, mass, and temperature conversions.
- `text_stats` - characters, words, lines, and sentences.
- `csv_preview` / `sqlite_query` - read-only previews of workspace data files.
- `search_notes` / `read_note` - search and read `data/notes/*.md`.
- `list_files` / `read_file` / `write_file` - sandboxed to `data/workspace`.

Resources: `notes://index`, `notes://{name}`, `config://agent`,
`workspace://index`, and `workspace://file/{+path}`.

Prompts: `research(topic)`, `explain_mcp()`.

Completions cover note names, workspace paths, and the `research` prompt topic.

Protocol features:

- `--progress` renders `notifications/progress` from long-running tools.
- `--server-logs` opts into MCP logging messages (`notifications/message`).
- `write_file` asks for approval through MCP elicitation by default. Because
  the 2026-07-28 protocol forbids server-initiated requests, the client uses
  the legacy handshake whenever an elicitation callback is installed.
  `--auto-approve-writes` (or `REACT_MCP_REQUIRE_WRITE_APPROVAL=false`) skips
  the prompt and keeps the modern handshake.

Try the protocol features directly:

```bash
# Progress + server logs from a real tool call
uv run react-mcp call csv_preview --args '{"path":"scores.csv"}' \
  --progress --server-logs

# Interactive approval prompt, then a second run with approval disabled
uv run react-mcp call write_file --args '{"path":"hello.txt","content":"hi"}'
uv run react-mcp call write_file --auto-approve-writes \
  --args '{"path":"hello.txt","content":"hi"}'

# Ask the agent to write a file; approve at the prompt
uv run react-mcp ask "Write hello.txt with a short greeting."
```

`SDKMCPClient` also exposes `complete_prompt()`, `complete_resource()`, and
`list_resource_templates()` for exploring completions and URI templates from
Python.

## Observability

Each run writes a folder under `traces/<timestamp>-<run-id>/`:

| File | Contents |
| --- | --- |
| `run.json` | Run id, start time, environment metadata. |
| `spans.jsonl` | One JSON object per span (nested, with durations). |
| `events.jsonl` | Standalone timestamped events. |
| `protocol.jsonl` | Raw MCP frames (protocol demo / raw client). |
| `summary.json` | Span counts, metrics, artifact paths. |

Typical spans: `agent.run` > `agent.step` > `llm.chat` and `mcp.tools/call`.
Metrics include steps, LLM calls, prompt/completion tokens, tool calls, tool
errors, parse retries, and wall time. `--otel console` or `--otel otlp` also
exports spans through OpenTelemetry; install with `uv sync --all-extras`.

```bash
ls traces
jq -c '{name, ms:.duration_ms, status}' traces/*/spans.jsonl
```

## Configuration

Settings come from environment variables with the `REACT_MCP_` prefix, loaded
from `.env` when present. CLI flags override them.

| Variable | Default | Purpose |
| --- | --- | --- |
| `REACT_MCP_MODEL` | `qwen3.5:9b` | Ollama model. |
| `REACT_MCP_OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint. |
| `REACT_MCP_TEMPERATURE` | `0.1` | Sampling temperature. |
| `REACT_MCP_NUM_CTX` | `8192` | Context window. |
| `REACT_MCP_KEEP_ALIVE` | `10m` | Ollama model keep-alive duration. |
| `REACT_MCP_THINK` | `false` | Enable thinking-channel output. |
| `REACT_MCP_REQUEST_TIMEOUT` | `180` | MCP request timeout in seconds. |
| `REACT_MCP_MAX_STEPS` | `8` | Maximum ReAct iterations. |
| `REACT_MCP_MAX_PARSE_RETRIES` | `2` | Format-correction retries. |
| `REACT_MCP_MAX_OBSERVATION_CHARS` | `2000` | Observation truncation. |
| `REACT_MCP_MEMORY_TURNS` | `6` | Prior Q/A turns injected into follow-ups. |
| `REACT_MCP_MEMORY_CHARS` | `4000` | Total character budget for prior Q/A. |
| `REACT_MCP_REQUIRE_WRITE_APPROVAL` | `true` | Require MCP elicitation before `write_file`. |
| `REACT_MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `REACT_MCP_SERVER_URL` | `http://127.0.0.1:8000/mcp` | HTTP endpoint. |
| `REACT_MCP_SESSION_DIR` | `./data/sessions` | Saved REPL session directory. |
| `REACT_MCP_TRACE_DIR` | `./traces` | Trace output directory. |
| `REACT_MCP_LOG_LEVEL` | `INFO` | Log verbosity. |
| `REACT_MCP_OTEL` | `none` | `none`, `console`, or `otlp`. |
| `REACT_MCP_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP collector. |

Path settings (`REACT_MCP_WORKSPACE_DIR`, `REACT_MCP_NOTES_DIR`,
`REACT_MCP_SESSION_DIR`) are available too; see `.env.example`.

## Adding a tool

Add a decorated function in `src/react_mcp/tools.py`, then restart the client so
it can rediscover the tools:

```python
@mcp.tool(title="Word count")
@instrumented
def word_count(text: Annotated[str, Field(description="Text to count.")]) -> int:
    """Count whitespace-separated words."""
    return len(text.split())
```

`@instrumented` is a `functools.wraps` wrapper, so the MCP SDK still sees the
original signature and generates the JSON schema from the annotations.

For progress, logging, or elicitation, make the tool `async` and add a
`Context` parameter. The SDK injects it at call time and omits it from the
schema:

```python
from mcp.server.mcpserver import Context

@mcp.tool(title="Slow preview")
@instrumented
async def slow_preview(path: str, ctx: Context) -> str:
    await ctx.report_progress(0, total=2, message="opening")
    text = await asyncio.to_thread(read_file, path)
    await ctx.report_progress(2, total=2, message="done")
    return text
```

Use `ctx.elicit()` only when the client advertised elicitation support, and
return a clear `ToolError` when it did not.

## Project layout

```text
src/react_mcp/
  cli.py                 # all CLI subcommands
  config.py              # pydantic settings and paths
  server.py              # MCPServer construction and transports
  tools.py               # tools, resources, prompts, completions, sandbox
  agent/
    prompts.py           # ReAct system prompt and retry prompts
    parser.py            # forgiving Thought/Action/Final parser
    llm.py               # Ollama wrapper, tokens, friendly errors
    memory.py            # bounded Q/A memory and JSONL sessions
    react.py             # the loop
  mcp_client/
    base.py              # shared ToolSpec/ToolResult/Protocol
    sdk_client.py        # official mcp.Client wrapper
    raw_client.py        # hand-rolled JSON-RPC stdio client
  observability/
    tracer.py            # JSONL spans/events/protocol + OTel bridge
    metrics.py           # per-run counters
    render.py            # Rich output
    logging.py           # stderr logging
    otel.py              # optional exporter setup
data/notes/              # note corpus served over MCP resources/tools
data/sessions/           # saved Q/A history (gitignored)
data/workspace/          # the only writable directory for file tools
scripts/demo_protocol.py # standalone raw-frame demo
tests/                   # unit + subprocess integration tests
docs/mcp-protocol.md     # protocol walkthrough
```

## Testing

```bash
uv run ruff check .
uv run ruff format --check src tests
uv run pytest -m "not integration"   # fast unit tests
uv run pytest                        # also launches real MCP subprocesses
uv run pytest -m integration         # only MCP protocol integration tests
```

Ollama is not required for the test suite; the agent tests use a scripted fake
LLM. The `doctor` command and `ask` are the Ollama-dependent checks.

## Troubleshooting

- **`Connection refused`** - start Ollama with `ollama serve`.
- **`model not found`** - run `ollama pull <model>` or pass `--model`.
- **Malformed action JSON** - lower the temperature, use a larger model, or let
  the built-in parse retry recover. `--think` can help reasoning at the cost of
  latency.
- **Context overflow** - raise `REACT_MCP_NUM_CTX` or lower `--max-steps`.
- **First call is slow** - the model is loading. Later calls reuse it for
  `REACT_MCP_KEEP_ALIVE`.
- **Inspector** - `uv run mcp dev src/react_mcp/server.py` opens the MCP
  Inspector for the module-level server.

## Safety

This is a learning project, not a hardened service. Tool arguments are treated
as untrusted: arithmetic is AST-whitelisted, file paths are resolved and checked
against the workspace root, and writes are size-limited. The HTTP transport has
no authentication and binds to loopback by default; do not expose it publicly
without adding auth and TLS.

See [docs/mcp-protocol.md](docs/mcp-protocol.md) for the protocol details and
[data/notes/](data/notes/) for the notes the agent can search.
