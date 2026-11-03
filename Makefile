.PHONY: sync test lint format check tools demo ask serve inspect clean

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

serve:
	uv run react-mcp serve

inspect:
	uv run mcp dev src/react_mcp/server.py

clean:
	rm -rf traces .pytest_cache .ruff_cache
