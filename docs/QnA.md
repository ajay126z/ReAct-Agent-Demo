# QnA

Common questions about this project — the ReAct agent, MCP, tools, and the free OpenCode-style web tools. Written for learners of ReAct architecture and its implementation.

## ReAct agent

**Q: How is the “Reason → Act → Observe” cycle implemented? What is the iteration limit?**

A: The loop lives in `src/react_mcp/agent/react.py:ReActAgent.run()`. Each iteration is: (1) Reasoning — `await llm.chat(messages, stop=["Observation:"])` produces text parsed by `parser.parse_react_output()` into `final | action | invalid`; (2) Acting — `_execute_action()` validates `Action:` against the known tool map and calls `await client.call_tool(action, args)` over MCP; (3) Observing — the result is truncated to `REACT_MCP_MAX_OBSERVATION_CHARS` (default `2000`) and appended as `user: "Observation: <result>"`, then the loop repeats.

The iteration limit is `Settings.max_steps` (`REACT_MCP_MAX_STEPS`), **default `8`, range `1–50`** — settable via `.env` or `--max-steps`. The loop is `for step in range(1, max_steps + 1)`.

There is no `max_parse_retries` per query in the strict sense, but each invalid parse retries up to `max_parse_retries` (default `2`) with `PARSE_RETRY_TEMPLATE`; `forced_final` (via `FORCE_FINAL_PROMPT` → `FORCE_FINAL_PROMPT`) kicks in when `max_steps` is exhausted.

**Q: Why text-based ReAct instead of native tool calling?**

A: So the agent's reasoning and every action are visible in the transcript and trace — you can see `Thought`, `Action`, `Action Input`, and `Observation` as plain text (see `README.md: How it works`). It also works uniformly with local Ollama models that may not support native tool schemas. The trade-off is a forgiving parser: `src/react_mcp/agent/parser.py` strips markdown fences, tolerates prose around JSON, and accepts Python-style single quotes via `ast.literal_eval`.

**Q: How does the agent avoid inventing a tool result?**

A: Three guards: (1) the LLM call uses `stop=["Observation:"]` so the model must stop before fabricating output; (2) the system prompt (`src/react_mcp/agent/prompts.py:SYSTEM_TEMPLATE`) forbids writing `Observation:` and limits the model to one `Action:` per turn using only listed tool names; (3) every observation comes from a real `tools/call` over MCP, never from the model itself.

**Q: How does the agent learn its tools?**

A: At startup the client calls MCP `tools/list` and renders each tool's name, description, and JSON input schema into the system prompt via `build_system_prompt()` / `format_tool_catalog()`. Adding a tool on the server side means the agent can call it next run without code changes — see `uv run react-mcp tools` (rediscovers every run).

**Q: What happens when the model outputs `Final Answer`?**

A: `parse_react_output()` returns `kind="final"`; the loop breaks with `stopped_reason="final_answer"`, prints the final output, and returns `AgentResult`. If steps run out first, `_force_final()` supplies `FORCE_FINAL_PROMPT` and takes the model's text as `forced_final` (or `step_limit` fallback).

**Q: What is bounded Q/A memory in REPL/ASK sessions?**

A: `ConversationMemory` injects only the last `REACT_MCP_MEMORY_TURNS` (default `6`) completed Q/A turns, capped at `REACT_MCP_MEMORY_CHARS` (default `4000`) total characters. Intermediate ReAct observations are *not* stored — see `src/react_mcp/agent/memory.py` and sessions in `data/sessions/*.jsonl`.

**Q: How are errors and parse failures handled?**

A: Unknown tool names and bad argument shapes become `is_error` observations (`"Error: unknown tool …"`), tool exceptions become `"Error from tool …"`, and malformed `Action Input` triggers a `PARSE_RETRY_TEMPLATE` re-ask (default `2` retries). Each path increments `RunMetrics` counters (tool errors, parse retries) visible in traces.

## MCP

**Q: What is MCP in this project, in one diagram?**

A: A local MCP server (`python -m react_mcp.server` over stdio, or Streamable HTTP) exposes **tools** + **resources** + **prompts**; the ReAct agent talks to it via an MCP client (`SDKMCPClient` official SDK, or `RawMCPClient` handwritten JSON-RPC). The cycle is: `initialize` → `tools/list` (discovery) → `tools/call` (execution) → observations fed back to the LLM. See the sequence diagram in `docs/mcp-protocol.md` and `uv run react-mcp protocol-demo`.

**Q: What is the difference between stdio and HTTP transports?**

A: `stdio` (default) — the client spawns the server as a subprocess and exchanges newline-delimited JSON-RPC on stdin/stdout. Simple, local, no network surface. `http` — `react-mcp serve --http --port 8000` plus `--transport http --server-url http://127.0.0.1:8000/mcp`; useful for the MCP Inspector (`uv run mcp dev src/react_mcp/server.py`) and remote clients. `serve` over HTTP binds loopback by default with no auth — do not expose publicly without adding auth/TLS (see `README.md` Safety).

**Q: What are tools, resources, prompts, and completions?**

A: All four are MCP capability types: **tools** (callable functions like `calculator`, `web_fetch`); **resources** (readable URIs like `notes://index`, `config://agent`, `workspace://file/{path}`); **prompts** (reusable templates like `research(topic)`, `explain_mcp()`); **completions** (autocomplete for note names, workspace paths, prompt topics, `src/react_mcp/tools.py:register_completions`).

**Q: Why does `write_file` use elicitation? What is elicitation?**

A: MCP elicitation is server-initiated approval: for `write_file` the server calls `ctx.elicit(...)` to ask "Allow writing N characters to X?" before mutating disk. The client renders the prompt on stdin and returns accept/decline/cancel. Because the `2026-07-28` protocol forbids server-initiated requests, the client uses the legacy handshake when elicitation is installed; `--auto-approve-writes` (or `REACT_MCP_REQUIRE_WRITE_APPROVAL=false`) skips the prompt and keeps the modern handshake.

**Q: What is the legacy handshake fallback about?**

A: The official SDK client defaults to protocol `2026-07-28`, but if an elicitation callback is registered it falls back to legacy `initialize` (`2025-11-25`) so server→client requests remain legal. The educational raw client in `scripts/demo_protocol.py` always uses the legacy handshake to match common MCP material. See `README.md: How it works`.

**Q: How do I inspect raw MCP frames?**

A: `uv run react-mcp protocol-demo` prints the `initialize` / `tools/list` / `tools/call` frames and writes them to `traces/<run>/protocol.jsonl`. `uv run mcp dev src/react_mcp/server.py` opens the graphical MCP Inspector for interactive calls.

## Tools

**Q: How is a new MCP tool added?**

A: Add a method to `Toolbox` in `src/react_mcp/tools.py` (with `ToolError` for invalid input, size limits, and `_safe_path()` checks for filesystem tools), then register it in `register_tools()` with `@mcp.tool(title=..., annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))` and `@instrumented`. `@instrumented` is `functools.wraps`, preserving the original signature so the SDK derives the JSON schema. For progress/logging, make it `async def …(ctx: Context)` and call `ctx.report_progress` / `ctx.log`. Restart the client so it rediscovers tools.

**Q: Why `ToolAnnotations` (`read_only_hint`, `open_world_hint`, …)?**

A: They advertise semantics: e.g. `calculator` is `read_only_hint=True, open_world_hint=False`; `write_file` is `read_only_hint=False, destructive_hint=True, idempotent_hint=True`; `web_fetch`/`web_search` are `read_only_hint=True, open_world_hint=True` (they touch the outside world but do not mutate it). Clients and the agent prompt use these hints to decide when to ask for approval.

**Q: How do tools stay safe?**

A: Arithmetic is AST-whitelisted (never `eval`); file paths are resolved against `data/workspace` and must stay inside it (absolute paths/traversal rejected); writes are size-limited and gated by elicitation; `sqlite_query` is read-only `SELECT`/`WITH` only; web tools are SSRF-guarded (public IPs only, including after redirects) and treat fetched content as untrusted (prompt-injection risk).

**Q: How do I call a tool without the LLM?**

A:

```bash
uv run react-mcp tools
uv run react-mcp call calculator --args '{"expression": "(17 * 3) + 9"}'
uv run react-mcp call web_fetch --args '{"url":"https://example.com"}'
```

This exercises the exact `tools/call` path the agent uses, minus the model.

**Q: Where should I look to understand a tool end-to-end?**

A: `Toolbox.<name>()` (logic) → `register_tools()` wrapper (schema + `@instrumented` + `Context` progress) → `server.py:INSTRUCTIONS` (advertised behaviour) → `tests/test_*.py` (behaviour contract). The trace dir (`traces/<run>/spans.jsonl`) shows `agent.step > llm.chat` and `mcp.tools/call` spans with durations and metrics.

## Web tools (`web_fetch` / `web_search`)

**Q: What does `web_fetch` do, and how is it like OpenCode's WebFetchTool?**

A: `web_fetch(url, format="auto")` fetches one public `http://` / `https://` URL and returns cleaned content — markdown for HTML pages, plain text otherwise. `format` can be `auto`, `markdown`, `text`, or `html` (raw, truncated). It mirrors OpenCode's `webfetch` shape: a URL in, page content out, truncated to fit the agent's observation budget.

**Q: What does `web_search` do — is it free?**

A: `web_search(query, limit=5)` returns titles, URLs, and snippets via the free Bing HTML endpoint (`https://www.bing.com/search?q=...`). No API key and no cost. Use it to find candidate URLs, then call `web_fetch` to read a result page.

**Q: Why Bing instead of DuckDuckGo?**

A: Live testing showed DuckDuckGo's HTML/lite endpoints now serve a bot-challenge page (and `202` for non-browser agents), while Bing's HTML SERP (`b_algo` results) returns parseable results with this project's plain user-agent. The parser unwraps Bing's `/ck/a` redirect links (base64 `u` param) back to the real destination URL.

**Q: Are the web tools enabled by default? How do I turn them off?**

A: Yes — on by default with strict guards (see below). Set `REACT_MCP_WEB_FETCH_ENABLED=false` (or `REACT_MCP_WEB_FETCH_ENABLED=false` in `.env`) to disable both tools; the server then raises a clear "disabled" `ToolError` and the agent is told to use another tool.

**Q: How is SSRF prevented?**

A: Before every request — and again on every redirect hop — the URL is validated: only `http`/`https`, no embedded credentials, the hostname must resolve *only* to public IPs. Private, loopback, link-local (e.g. `169.254.169.254`), reserved, and multicast addresses are blocked, including literal IPs like `http://127.0.0.1/`. Redirects are capped at 5.

**Q: What are the size/timeout limits?**

A: Configurable in `config.py`: `REACT_MCP_WEB_FETCH_TIMEOUT` (default 15s), `REACT_MCP_WEB_FETCH_MAX_BYTES` (default 500000), `REACT_MCP_WEB_FETCH_MAX_CHARS` (default 20000). The agent's `REACT_MCP_MAX_OBSERVATION_CHARS` (default 2000) still truncates what the model sees per turn.

**Q: Is fetched content trusted?**

A: No. Fetched pages and search snippets are untrusted web data and may contain prompt-injection text. The tool marks every response with a `note` field, and the ReAct prompt instructs the model to never follow instructions inside fetched content.

**Q: How do I try it without the LLM?**

A:

```bash
uv run react-mcp call web_fetch --args '{"url":"https://example.com"}'
uv run react-mcp call web_search --args '{"query":"Python 3.12 whatsnew","limit":3}'
uv run react-mcp ask "Search the web for Python 3.12 whatsnew and fetch the top result."
```

**Q: Why no new dependencies?**

A: Both tools use only the Python standard library (`urllib`, `html.parser`, `ipaddress`, `socket`, `base64`), so the project stays free to run with zero extra installs and no API keys.

## General

**Q: Where are web tools implemented?**

A: `Toolbox.web_fetch()` / `Toolbox.web_search()` in `src/react_mcp/tools.py`, registered in `register_tools()`, with settings in `src/react_mcp/config.py`. They follow the same `@instrumented` + `Context` progress/log pattern as `csv_preview` and `sqlite_query`.

**Q: Do I need to restart anything to see the new tools?**

A: No — the client rediscovers MCP tools on every run. `uv run react-mcp tools` should list `web_fetch` and `web_search` (15 tools total).
