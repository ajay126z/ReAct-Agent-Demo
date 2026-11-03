"""Tool, resource, and prompt implementations for the MCP server.

Everything here is deliberately local and deterministic: no network access, no
arbitrary code execution, and all file access is confined to a workspace root.
"""

from __future__ import annotations

import ast
import functools
import json
import operator
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings
from .observability.logging import get_logger

log = get_logger("tools")

MAX_EXPRESSION_CHARS = 256
MAX_EXPONENT = 100
MAX_ABS_RESULT = 1e100
MAX_WRITE_CHARS = 100_000

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")


def _summarize(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def instrumented(fn):
    """Log tool entry/exit on the server's stderr without changing its signature."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        log.info("tool.start name=%s args=%s", fn.__name__, _summarize(kwargs))
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            log.warning(
                "tool.error name=%s duration_ms=%.1f error=%s: %s",
                fn.__name__,
                duration,
                type(exc).__name__,
                exc,
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        log.info("tool.end name=%s duration_ms=%.1f", fn.__name__, duration)
        return result

    return wrapper


class Toolbox:
    """Stateful implementation behind the MCP tools."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()

    # -- calculator -------------------------------------------------------
    def calculator(self, expression: str) -> str:
        expression = expression.strip()
        if not expression:
            raise ToolError("The expression is empty.")
        if len(expression) > MAX_EXPRESSION_CHARS:
            raise ToolError(f"Expression is too long (max {MAX_EXPRESSION_CHARS} characters).")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ToolError(f"Could not parse expression: {exc.msg}") from exc

        value = self._eval_node(tree.body)
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ToolError("Result is not a finite number.")
            if abs(value) > MAX_ABS_RESULT:
                raise ToolError("Result is too large.")
            return format(value, ".12g")
        return str(value)

    def _eval_node(self, node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ToolError("Only integer and float constants are allowed.")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_EXPONENT:
                    raise ToolError(f"Exponent is too large (max {MAX_EXPONENT}).")
                if abs(left) > 1e6:
                    raise ToolError("Base is too large for exponentiation.")
            try:
                result = _BIN_OPS[type(node.op)](left, right)
            except ZeroDivisionError as exc:
                raise ToolError("Division by zero.") from exc
            if isinstance(result, (int, float)) and abs(result) > MAX_ABS_RESULT:
                raise ToolError("Intermediate result is too large.")
            return result
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](self._eval_node(node.operand))
        raise ToolError(
            f"Unsupported expression element: {type(node).__name__}. "
            "Only numbers and + - * / // % ** parentheses are allowed."
        )

    # -- time -------------------------------------------------------------
    def current_time(self, timezone: str) -> str:
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ToolError(
                f"Unknown timezone {timezone!r}. Try 'UTC' or 'America/New_York'."
            ) from exc
        now = datetime.now(zone)
        return now.isoformat(timespec="seconds") + f" ({now.strftime('%A')})"

    # -- notes ------------------------------------------------------------
    def note_files(self) -> list[Path]:
        if not self.settings.notes_dir.exists():
            return []
        return sorted(path for path in self.settings.notes_dir.glob("*.md") if path.is_file())

    def note_title(self, path: Path) -> str:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    return line[2:].strip()
        except OSError:
            pass
        return path.stem.replace("_", " ").replace("-", " ").title()

    def note_index(self) -> list[dict[str, str]]:
        return [{"name": path.stem, "title": self.note_title(path)} for path in self.note_files()]

    def _note_path(self, name: str) -> Path:
        stem = name[:-3] if name.lower().endswith(".md") else name
        if not _NAME_RE.match(stem) or ".." in stem:
            raise ToolError(f"Invalid note name {name!r}.")
        path = (self.settings.notes_dir / f"{stem}.md").resolve()
        root = self.settings.notes_dir.resolve()
        if root != path and root not in path.parents:
            raise ToolError("Note path escapes the notes directory.")
        if not path.is_file():
            available = ", ".join(item["name"] for item in self.note_index()) or "none"
            raise ToolError(f"No note named {stem!r}. Available notes: {available}.")
        return path

    def read_note(self, name: str) -> str:
        return self._note_path(name).read_text(encoding="utf-8")

    def search_notes(self, query: str, limit: int) -> list[dict[str, Any]]:
        terms = re.findall(r"[a-z0-9]+", query.lower())
        if not terms:
            raise ToolError("Search query must contain at least one word.")
        results: list[dict[str, Any]] = []
        for path in self.note_files():
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            title = self.note_title(path)
            haystack = f"{title}\n{body}".lower()
            title_lower = title.lower()
            score = sum((3 * title_lower.count(term)) + haystack.count(term) for term in set(terms))
            if score == 0:
                continue
            lowered = body.lower()
            position = min(
                (lowered.find(term) for term in terms if lowered.find(term) >= 0),
                default=0,
            )
            start = max(position - 60, 0)
            snippet = body[start : start + 200].replace("\n", " ").strip()
            results.append(
                {
                    "name": path.stem,
                    "title": title,
                    "score": score,
                    "snippet": snippet + ("..." if start + 200 < len(body) else ""),
                }
            )
        results.sort(key=lambda item: (-item["score"], item["name"]))
        return results[:limit]

    # -- filesystem sandbox ----------------------------------------------
    def _safe_path(self, relative: str, *, must_exist: bool = False) -> Path:
        if "\x00" in relative:
            raise ToolError("Path contains a null byte.")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ToolError("Absolute paths are not allowed; use paths relative to the workspace.")
        root = self.settings.workspace_dir.resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise ToolError("Path escapes the workspace directory.")
        if must_exist and not resolved.exists():
            raise ToolError(f"Path does not exist: {relative!r}")
        return resolved

    def list_files(self, relative: str) -> list[dict[str, Any]]:
        target = self._safe_path(relative or ".", must_exist=True)
        root = self.settings.workspace_dir.resolve()
        if target.is_file():
            return [
                {
                    "path": target.relative_to(root).as_posix(),
                    "type": "file",
                    "size": target.stat().st_size,
                }
            ]
        entries: list[dict[str, Any]] = []
        for path in sorted(target.rglob("*")):
            if len(entries) >= 200:
                entries.append({"path": "...", "type": "truncated", "size": 0})
                break
            if path.is_file():
                entries.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "type": "file",
                        "size": path.stat().st_size,
                    }
                )
            elif path.is_dir():
                entries.append(
                    {"path": path.relative_to(root).as_posix(), "type": "dir", "size": 0}
                )
        return entries

    def read_file(self, relative: str, max_bytes: int) -> str:
        path = self._safe_path(relative, must_exist=True)
        if not path.is_file():
            raise ToolError(f"Not a file: {relative!r}")
        raw = path.read_bytes()
        truncated = len(raw) > max_bytes
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n[truncated at {max_bytes} bytes; file is {len(raw)} bytes]"
        return text

    def write_file(self, relative: str, content: str) -> str:
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"Content is too large (max {MAX_WRITE_CHARS} characters).")
        path = self._safe_path(relative)
        if path == self.settings.workspace_dir.resolve():
            raise ToolError("Refusing to overwrite the workspace directory.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        relative = path.relative_to(self.settings.workspace_dir.resolve()).as_posix()
        return f"Wrote {len(content)} characters to {relative}."


def register_tools(mcp: MCPServer, toolbox: Toolbox) -> None:
    """Register every MCP tool on the server."""

    @mcp.tool(
        title="Calculator",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def calculator(
        expression: Annotated[
            str,
            Field(
                description=(
                    "Arithmetic expression using numbers, parentheses, and + - * / // % **."
                ),
                examples=["(17 * 3) + 9", "2 ** 10", "1 / 3"],
            ),
        ],
    ) -> str:
        """Evaluate a basic arithmetic expression safely."""
        return toolbox.calculator(expression)

    @mcp.tool(
        title="Current time",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def get_current_time(
        timezone: Annotated[
            str,
            Field(description="IANA timezone name, for example 'UTC' or 'America/New_York'."),
        ] = "UTC",
    ) -> str:
        """Return the current date and time in a timezone."""
        return toolbox.current_time(timezone)

    @mcp.tool(
        title="Search notes",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def search_notes(
        query: Annotated[str, Field(description="Words to search for in the local notes corpus.")],
        limit: Annotated[int, Field(ge=1, le=20, description="Maximum number of matches.")] = 5,
    ) -> list[dict[str, Any]]:
        """Search the bundled notes and return ranked matches with snippets."""
        return toolbox.search_notes(query, limit)

    @mcp.tool(
        title="Read note",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def read_note(
        name: Annotated[
            str,
            Field(description="Note name without the .md suffix, as returned by search_notes."),
        ],
    ) -> str:
        """Read the full Markdown content of one note."""
        return toolbox.read_note(name)

    @mcp.tool(
        title="List workspace files",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def list_files(
        path: Annotated[str, Field(description="Directory relative to the workspace root.")] = "",
    ) -> list[dict[str, Any]]:
        """List files under the sandboxed workspace directory."""
        return toolbox.list_files(path)

    @mcp.tool(
        title="Read workspace file",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def read_file(
        path: Annotated[str, Field(description="File path relative to the workspace root.")],
        max_bytes: Annotated[int, Field(ge=1, le=100_000)] = 20_000,
    ) -> str:
        """Read a UTF-8 text file from the sandboxed workspace."""
        return toolbox.read_file(path, max_bytes)

    @mcp.tool(
        title="Write workspace file",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True
        ),
    )
    @instrumented
    def write_file(
        path: Annotated[str, Field(description="File path relative to the workspace root.")],
        content: Annotated[str, Field(description="Full text content to write.")],
    ) -> str:
        """Write a UTF-8 text file inside the sandboxed workspace."""
        return toolbox.write_file(path, content)


def register_resources(mcp: MCPServer, toolbox: Toolbox, settings: Settings) -> None:
    @mcp.resource("notes://index", name="note-index", mime_type="application/json")
    def notes_index() -> str:
        """JSON index of every note available to the agent."""
        return json.dumps(toolbox.note_index(), indent=2)

    @mcp.resource("notes://{name}", name="note", mime_type="text/markdown")
    def note_contents(name: str) -> str:
        """The Markdown body of one note."""
        return toolbox.read_note(name)

    @mcp.resource("config://agent", name="agent-config", mime_type="application/json")
    def agent_config() -> str:
        """Current server configuration (paths, model defaults, observability)."""
        return json.dumps(settings.redacted(), indent=2)


def register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(title="Research a topic")
    def research(topic: str) -> str:
        """Ask the agent to research a topic using the notes corpus."""
        return (
            f"Research {topic!r} using the local notes. Start with search_notes, read the most "
            "relevant notes, and then give a concise answer with the note names you used."
        )

    @mcp.prompt(title="Explain MCP")
    def explain_mcp() -> str:
        """Ask for a short explanation of the MCP request lifecycle."""
        return (
            "Explain the MCP lifecycle from client to server: transport, initialization, "
            "capability negotiation, and a tools/call request. Keep it under 250 words."
        )
