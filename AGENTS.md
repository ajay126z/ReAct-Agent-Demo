# AGENTS.md

Local ReAct + MCP learning project. `README.md` is the user-facing guide,
`docs/QnA.md` explains design decisions, and `docs/mcp-protocol.md` is the
frame-level protocol walkthrough. Read those before changing architecture or
protocol behavior.

## Commands

Run project commands through `uv` (Python 3.12+), not a bare `python` or
`pytest`.

- Setup: `uv sync --all-extras`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format src tests scripts`
- Format check: `uv run ruff format --check src tests scripts`
- Fast tests: `uv run pytest -q -m "not integration"`
- All tests: `uv run pytest -q`
- Integration only: `uv run pytest -q -m integration`
- One test: `uv run pytest -q tests/test_web_fetch.py::test_web_fetch_markdown`

**Never run `ruff format .` or `ruff format --check .`.** The tracked repo-root
`session-ses_20261103T165700.md` conversation dump and Markdown code blocks in
`README.md` are reformatted by this Ruff version, so a whole-repo check fails for
unrelated reasons. Keep the path-scoped command. `make check` runs lint plus
tests, not the format check.

There is no CI, type checker, or pre-commit config; local verification is the
contract. Ollama is **not** needed for tests. `doctor`, `ask`, and `repl` do need
a running Ollama with the configured model.

## Chat vs. server

- `uv run react-mcp repl` is the terminal chat UI.
- `uv run react-mcp ask "..."` is one-shot.
- `uv run react-mcp serve` is the MCP server transport, **not** a chat UI.
- `uv run react-mcp call <tool> --args '<json>'` exercises one tool without the LLM.
- `make chat`, `make chat-debug`, `make chat-session SESSION=name`, and the other
  `chat-*` targets wrap common REPL flags (see `Makefile`).

Session flags: `-s/--session NAME` appends completed Q/A turns to
`data/sessions/NAME.jsonl`; `-c/--continue` resumes the newest; no flag is
in-process only. Only final Q/A pairs persist, not ReAct observations.

## Architecture

- `src/react_mcp/cli.py` is the host application and subcommand dispatcher.
- `SDKMCPClient` launches `python -m react_mcp.server` over stdio by default, or
  connects to `serve --http`. `RawMCPClient` is for the protocol demo and raw
  JSON-RPC teaching; the agent does not use it.
- The agent depends only on `MCPToolClient` in `mcp_client/base.py` (`list_tools`
  plus `call_tool`). Resources, prompts, and completions are exploration
  features, not part of the ReAct loop.
- The loop is text ReAct, not native tool calling. Preserve the
  `stop=["Observation:"]` contract, the forgiving parser, and the `Final Answer:`
  format or agent tests will break.
- `server.py:create_server()` is the single registration point. The module-level
  `mcp = create_server()` exists so `mcp dev src/react_mcp/server.py` works.
- stdio is the default transport. `serve --http` binds `127.0.0.1` with no auth;
  do not expose it publicly.

## Configuration and paths

- `Settings` uses the `REACT_MCP_` prefix and loads `.env` from the working
  directory; CLI flags override.
- `PROJECT_ROOT` is derived from `config.py`, with defaults under `data/`
  (`workspace`, `notes`, `sessions`) and `traces/`. Relative path settings resolve
  against the current working directory, so run commands from the repo root or use
  absolute paths.
- `data/workspace/` is the only writable area for file tools; `data/notes/` is
  the note corpus. File arguments are workspace-relative and reject absolute
  paths and traversal.
- **Adding a server setting?** Put it in `Settings` and pass it through
  `Settings.server_env()`. That method is how CLI overrides reach the stdio child;
  the child otherwise sees only defaults and `.env`.
- `data/notes/*.md` are served over MCP, so edits there change agent behavior and
  are user-visible. Keep `server.py:INSTRUCTIONS` aligned when adding tools.

## Tools and MCP features

- Implement logic on `Toolbox` in `tools.py`, then register it in
  `register_tools()`. Decorator order matters:

```python
@mcp.tool(title="...", annotations=ToolAnnotations(read_only_hint=True))
@instrumented
def my_tool(expression: str) -> str:
    return expression
```

`@instrumented` must be closest to the function. It preserves the signature via
`functools.wraps`, so the SDK's JSON schema stays clean.

- For progress, server logs, or elicitation, make the tool `async` and add a
  `ctx: Context` parameter. The SDK injects it and hides it from the tool schema
  (covered by `tests/test_mcp_phase3.py`). Wrap blocking I/O with
  `asyncio.to_thread`.
- Completion handlers must be `async`. A sync handler returns a `Completion` that
  the SDK tries to await, producing
  `TypeError: object Completion can't be used in 'await' expression`.
- Resource templates use `{+path}` for nested paths. `{path*}` is rejected by the
  SDK.
- `write_file` approval is on by default. The client automatically switches to
  the legacy `2025-11-25` handshake when an elicitation callback is installed,
  because protocol `2026-07-28` forbids server-initiated requests
  (`NoBackChannelError`). `tools` deliberately installs no elicitation and stays
  modern. Escape hatches: `--auto-approve-writes` or
  `REACT_MCP_REQUIRE_WRITE_APPROVAL=false`; without elicitation capability
  `write_file` returns a `ToolError` instead of writing.
- `web_fetch` / `web_search` are real network tools (stdlib `urllib` plus Bing
  HTML), enabled by default and guarded by SSRF checks. The opening "no network
  tools" line in `README.md` is stale. Disable them with
  `REACT_MCP_WEB_FETCH_ENABLED=false`.

## Tests

- Use the `settings` fixture from `tests/conftest.py`. It creates isolated
  temporary workspace, notes, sessions, and traces plus `alpha`/`beta` notes; do
  not write into the repo's `data/` directories from tests.
- `pytest-asyncio` runs in auto mode: plain `async def test_...` works; no
  `@pytest.mark.asyncio` needed.
- Mark tests that launch a real server or subprocess with
  `@pytest.mark.integration`. They must stay runnable without Ollama.
- Agent tests use `FakeLLM`/`FakeClient`; assert prompts, actions, and
  observations without a model.
- Web tests monkeypatch `_fetch_bytes` and `_assert_public_url`. Do not add tests
  that hit the live network or depend on Bing's markup.

## Docs

When changing the CLI, tools, protocol features, or config, update the relevant
files: `README.md`, `docs/QnA.md`, `docs/mcp-protocol.md`, and
`data/notes/welcome.md` (served to the agent). `docs/mcp-protocol.md` shows exact
JSON-RPC frames, so keep its examples consistent with actual behavior.
