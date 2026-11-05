.PHONY: sync test lint format check tools demo ask chat chat-quiet chat-session chat-continue chat-debug chat-auto serve inspect clean

SESSION ?=
REPL_ARGS ?=

sync:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

check:
	uv run ruff check . && uv run pytest

tools:
	uv run react-mcp tools

demo:
	uv run react-mcp protocol-demo

ask:
	uv run react-mcp ask "What is (17 * 3) + 9, and what time is it in UTC?"

chat:
	uv run react-mcp repl $(REPL_ARGS)

chat-quiet:
	uv run react-mcp repl --quiet

chat-session:
	@test -n "$(SESSION)" || (echo "Usage: make chat-session SESSION=research"; exit 1)
	uv run react-mcp repl --session "$(SESSION)"

chat-continue:
	uv run react-mcp repl --continue

chat-debug:
	uv run react-mcp repl --progress --server-logs

chat-auto:
	uv run react-mcp repl --auto-approve-writes

serve:
	uv run react-mcp serve

inspect:
	uv run mcp dev src/react_mcp/server.py

clean:
	rm -rf traces .pytest_cache .ruff_cache
