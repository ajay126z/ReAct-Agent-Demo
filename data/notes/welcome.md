# Welcome to the ReAct + MCP workspace

This project wires four pieces together:

1. **Ollama** runs a local model such as `qwen3.5:9b`.
2. **A ReAct agent** prompts the model to alternate between Thought and Action.
3. **An MCP client** discovers and calls tools on a server.
4. **An MCP server** implements those tools, resources, and prompts locally.

## What the tools can do

- `calculator` - safe arithmetic, no `eval`.
- `get_current_time` - current time in an IANA timezone.
- `search_notes` / `read_note` - search and read the Markdown notes in
  `data/notes`.
- `list_files` / `read_file` / `write_file` - work inside `data/workspace`, with
  path traversal rejected.

Resources expose the note index (`notes://index`), individual notes
(`notes://<name>`), and the redacted agent config (`config://agent`). Prompts
include `research` and `explain_mcp`.

## Try these questions

- "What is (17 * 3) + 9, and what time is it in UTC?"
- "Search the notes for observability and summarize what a span is."
- "Read the react note and explain why this agent uses text instead of native
  tool calling."
- "List the workspace files, then create `hello.txt` with a short greeting."

## Where things live

- `src/react_mcp/server.py` - MCP server and tool registration.
- `src/react_mcp/agent/` - prompts, parser, Ollama wrapper, ReAct loop.
- `src/react_mcp/mcp_client/` - official-SDK client and a raw JSON-RPC client.
- `src/react_mcp/observability/` - tracing, metrics, and Rich rendering.
- `traces/` - one folder per run.
- `data/workspace/` - the only place file tools may write.
