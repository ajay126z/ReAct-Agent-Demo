# Welcome to the ReAct + MCP workspace

This project wires four pieces together:

1. **Ollama** runs a local model such as `qwen3.5:9b`.
2. **A ReAct agent** prompts the model to alternate between Thought and Action.
3. **An MCP client** discovers and calls tools on a server.
4. **An MCP server** implements those tools, resources, and prompts locally.

## What the tools can do

- `calculator` - safe arithmetic, no `eval`.
- `get_current_time` - current time in an IANA timezone.
- `date_diff` / `add_days` - ISO date and datetime arithmetic.
- `unit_convert` - length, mass, and temperature conversions.
- `text_stats` - characters, words, lines, and sentences.
- `csv_preview` / `sqlite_query` - read-only previews of files in
  `data/workspace`.
- `search_notes` / `read_note` - search and read the Markdown notes in
  `data/notes`.
- `list_files` / `read_file` / `write_file` - work inside `data/workspace`, with
  path traversal rejected.

Resources expose the note index (`notes://index`), individual notes
(`notes://<name>`), the redacted agent config (`config://agent`), a workspace
file index (`workspace://index`), and workspace files
(`workspace://file/{+path}`). Prompts include `research` and `explain_mcp`.
Completions cover note names, workspace paths, and the `research` topic.

`csv_preview` and `sqlite_query` emit MCP progress while they work. Server logs
are available when the client opts in. `write_file` asks for approval through
MCP elicitation unless the client passes `--auto-approve-writes`.

Saved REPL sessions keep completed Q/A turns in `data/sessions/`. Use
`react-mcp repl --session NAME` to resume one, or `--continue` for the most
recent session. Intermediate observations are never persisted.

## Try these questions

- "What is (17 * 3) + 9, and what time is it in UTC?"
- "Search the notes for observability and summarize what a span is."
- "Read the react note and explain why this agent uses text instead of native
  tool calling."
- "List the workspace files, then create `hello.txt` with a short greeting."
- "How many hours are between 2026-01-01 and 2026-01-02?"
- "Convert 100 Fahrenheit to Celsius, then count the words in this question."

## Where things live

- `src/react_mcp/server.py` - MCP server and tool registration.
- `src/react_mcp/agent/` - prompts, parser, Ollama wrapper, ReAct loop.
- `src/react_mcp/mcp_client/` - official-SDK client and a raw JSON-RPC client.
- `src/react_mcp/observability/` - tracing, metrics, and Rich rendering.
- `traces/` - one folder per run.
- `data/workspace/` - the only place file tools may write.
