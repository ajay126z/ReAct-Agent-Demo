"""Tool, resource, and prompt implementations for the MCP server.

Everything here is deliberately local and deterministic: no network access, no
arbitrary code execution, and all file access is confined to a workspace root.
"""

from __future__ import annotations

import ast
import asyncio
import csv
import functools
import inspect
import json
import operator
import re
import sqlite3
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPDeprecationWarning
from mcp.types import (
    Completion,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from .config import Settings
from .observability.logging import get_logger

log = get_logger("tools")

MAX_EXPRESSION_CHARS = 256
MAX_EXPONENT = 100
MAX_ABS_RESULT = 1e100
MAX_WRITE_CHARS = 100_000
MAX_TEXT_CHARS = 100_000
MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 100
MAX_SQLITE_BYTES = 50_000_000
MAX_SQLITE_ROWS = 200


class WriteApproval(BaseModel):
    approve: bool = Field(description="Set to true to allow writing the file.")


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LENGTH_TO_METERS = {
    "m": 1.0,
    "km": 1000.0,
    "cm": 0.01,
    "mm": 0.001,
    "mi": 1609.344,
    "yd": 0.9144,
    "ft": 0.3048,
    "in": 0.0254,
}
_MASS_TO_KG = {
    "kg": 1.0,
    "g": 0.001,
    "mg": 0.000001,
    "lb": 0.45359237,
    "oz": 0.028349523125,
}
_TEMPERATURE_UNITS = {"c", "f", "k"}
_UNIT_ALIASES = {
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "km": "km",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "mi": "mi",
    "mile": "mi",
    "miles": "mi",
    "yd": "yd",
    "yard": "yd",
    "yards": "yd",
    "ft": "ft",
    "foot": "ft",
    "feet": "ft",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "mg": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "c": "c",
    "°c": "c",
    "celsius": "c",
    "centigrade": "c",
    "f": "f",
    "°f": "f",
    "fahrenheit": "f",
    "k": "k",
    "kelvin": "k",
}
_DIFF_UNIT_FACTORS = {
    "days": 86400.0,
    "hours": 3600.0,
    "minutes": 60.0,
    "seconds": 1.0,
}
_DIFF_UNIT_ALIASES = {
    "d": "days",
    "day": "days",
    "days": "days",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
}

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


def _tidy_number(value: float) -> int | float:
    if abs(value) < 1e-12:
        return 0
    if value.is_integer():
        return int(value)
    return float(f"{value:.12g}")


def _parse_temporal(value: str, label: str) -> datetime:
    text = value.strip()
    if not text:
        raise ToolError(f"{label} is empty.")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(
            f"{label} must be an ISO 8601 date or datetime, for example "
            "2026-01-31 or 2026-01-31T12:30:00+00:00."
        ) from exc


def _normalize_unit(unit: str) -> str:
    key = unit.strip().lower()
    normalized = _UNIT_ALIASES.get(key)
    if normalized is None:
        allowed = ", ".join(sorted(set(_UNIT_ALIASES.values())))
        raise ToolError(f"Unknown unit {unit!r}. Supported units: {allowed}.")
    return normalized


def _convert_units(value: float, source: str, target: str) -> float:
    if source in _LENGTH_TO_METERS and target in _LENGTH_TO_METERS:
        return value * _LENGTH_TO_METERS[source] / _LENGTH_TO_METERS[target]
    if source in _MASS_TO_KG and target in _MASS_TO_KG:
        return value * _MASS_TO_KG[source] / _MASS_TO_KG[target]
    if source in _TEMPERATURE_UNITS and target in _TEMPERATURE_UNITS:
        if source == "c":
            celsius = value
        elif source == "f":
            celsius = (value - 32.0) * 5.0 / 9.0
        else:
            celsius = value - 273.15
        if target == "c":
            return celsius
        if target == "f":
            return celsius * 9.0 / 5.0 + 32.0
        return celsius + 273.15
    raise ToolError(f"Cannot convert {source!r} to {target!r}; they are different unit types.")


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, memoryview):
        return value.tobytes().hex()
    return value


def _loggable_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in {"ctx", "context"}}


def _log_tool_error(name: str, started: float, exc: Exception) -> None:
    duration = (time.perf_counter() - started) * 1000
    log.warning(
        "tool.error name=%s duration_ms=%.1f error=%s: %s",
        name,
        duration,
        type(exc).__name__,
        exc,
    )


def instrumented(fn):
    """Log tool entry/exit without changing the wrapped function's signature."""

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            log.info(
                "tool.start name=%s args=%s",
                fn.__name__,
                _summarize(_loggable_args(kwargs)),
            )
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                _log_tool_error(fn.__name__, started, exc)
                raise
            duration = (time.perf_counter() - started) * 1000
            log.info("tool.end name=%s duration_ms=%.1f", fn.__name__, duration)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        log.info(
            "tool.start name=%s args=%s",
            fn.__name__,
            _summarize(_loggable_args(kwargs)),
        )
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            _log_tool_error(fn.__name__, started, exc)
            raise
        duration = (time.perf_counter() - started) * 1000
        log.info("tool.end name=%s duration_ms=%.1f", fn.__name__, duration)
        return result

    return wrapper


async def _server_log(ctx: Context, level: str, data: Any) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MCPDeprecationWarning)
        await ctx.log(level, data, logger_name="react_mcp.tools")


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

    # -- dates and units --------------------------------------------------
    def date_diff(self, start: str, end: str, unit: str) -> dict[str, Any]:
        start_dt = _parse_temporal(start, "start")
        end_dt = _parse_temporal(end, "end")
        if (start_dt.tzinfo is None) != (end_dt.tzinfo is None):
            raise ToolError("start and end must both include a timezone or both omit it.")
        normalized_unit = _DIFF_UNIT_ALIASES.get(unit.strip().lower())
        if normalized_unit is None:
            raise ToolError("unit must be days, hours, minutes, or seconds.")
        seconds = (end_dt - start_dt).total_seconds()
        value = _tidy_number(seconds / _DIFF_UNIT_FACTORS[normalized_unit])
        return {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "value": value,
            "unit": normalized_unit,
        }

    def add_days(self, date: str, days: int) -> str:
        parsed = _parse_temporal(date, "date")
        result = parsed + timedelta(days=days)
        if _DATE_ONLY_RE.match(date.strip()):
            return result.date().isoformat()
        return result.isoformat()

    def unit_convert(self, value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
        source = _normalize_unit(from_unit)
        target = _normalize_unit(to_unit)
        converted = _convert_units(float(value), source, target)
        return {
            "value": _tidy_number(converted),
            "unit": target,
            "from": source,
        }

    def text_stats(self, text: str) -> dict[str, Any]:
        if len(text) > MAX_TEXT_CHARS:
            raise ToolError(f"Text is too long (max {MAX_TEXT_CHARS} characters).")
        stripped = text.strip()
        sentences = len(re.findall(r"[^.!?]+[.!?]+", stripped))
        if stripped and not sentences:
            sentences = 1
        return {
            "characters": len(text),
            "characters_no_whitespace": len(re.sub(r"\s", "", text)),
            "words": len(text.split()),
            "lines": len(text.splitlines()),
            "sentences": sentences,
        }

    # -- tabular data -----------------------------------------------------
    def csv_preview(self, relative: str, rows: int) -> dict[str, Any]:
        if rows < 1 or rows > MAX_CSV_ROWS:
            raise ToolError(f"rows must be between 1 and {MAX_CSV_ROWS}.")
        path = self._safe_path(relative, must_exist=True)
        if not path.is_file():
            raise ToolError(f"Not a file: {relative!r}")
        if path.stat().st_size > MAX_CSV_BYTES:
            raise ToolError(f"CSV is too large (max {MAX_CSV_BYTES} bytes).")

        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample) if sample else csv.excel
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(handle, dialect)
            try:
                columns = next(reader)
            except StopIteration:
                columns = []
                data: list[list[str]] = []
                truncated = False
            else:
                data = []
                truncated = False
                for index, row in enumerate(reader):
                    if index >= rows:
                        truncated = True
                        break
                    data.append(row)

        return {
            "path": path.relative_to(self.settings.workspace_dir.resolve()).as_posix(),
            "columns": columns,
            "rows": data,
            "truncated": truncated,
        }

    def sqlite_query(self, relative: str, sql: str, limit: int) -> dict[str, Any]:
        if limit < 1 or limit > MAX_SQLITE_ROWS:
            raise ToolError(f"limit must be between 1 and {MAX_SQLITE_ROWS}.")
        statement = sql.strip()
        if not statement:
            raise ToolError("SQL query is empty.")
        first_word = statement.split(None, 1)[0].lower()
        if first_word not in {"select", "with"}:
            raise ToolError("Only read-only SELECT or WITH queries are allowed.")

        path = self._safe_path(relative, must_exist=True)
        if not path.is_file():
            raise ToolError(f"Not a file: {relative!r}")
        if path.stat().st_size > MAX_SQLITE_BYTES:
            raise ToolError(f"SQLite database is too large (max {MAX_SQLITE_BYTES} bytes).")

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
            cursor = connection.execute(statement)
            if cursor.description is None:
                raise ToolError("Query did not return rows.")
            columns = [item[0] for item in cursor.description]
            fetched = cursor.fetchmany(limit + 1)
        except sqlite3.Error as exc:
            raise ToolError(f"SQLite query failed: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

        truncated = len(fetched) > limit
        rows = [[_sqlite_value(value) for value in row] for row in fetched[:limit]]
        return {
            "path": path.relative_to(self.settings.workspace_dir.resolve()).as_posix(),
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
        }

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

    def workspace_index(self) -> list[dict[str, Any]]:
        return [entry for entry in self.list_files("") if entry["type"] == "file"]

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
        title="Date difference",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def date_diff(
        start: Annotated[
            str,
            Field(description="Start date or datetime in ISO 8601, e.g. 2026-01-01."),
        ],
        end: Annotated[
            str,
            Field(description="End date or datetime in ISO 8601, e.g. 2026-02-01."),
        ],
        unit: Annotated[
            str,
            Field(description="Difference unit: days, hours, minutes, or seconds."),
        ] = "days",
    ) -> dict[str, Any]:
        """Return the difference between two ISO dates or datetimes."""
        return toolbox.date_diff(start, end, unit)

    @mcp.tool(
        title="Add days",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def add_days(
        date: Annotated[
            str,
            Field(description="ISO 8601 date or datetime to shift, e.g. 2026-01-31."),
        ],
        days: Annotated[
            int,
            Field(
                description="Number of days to add; negative values subtract.",
                ge=-365000,
                le=365000,
            ),
        ],
    ) -> str:
        """Add or subtract days from an ISO date or datetime."""
        return toolbox.add_days(date, days)

    @mcp.tool(
        title="Convert units",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def unit_convert(
        value: Annotated[float, Field(description="Numeric value to convert.")],
        from_unit: Annotated[
            str,
            Field(description="Source unit, e.g. m, km, kg, lb, C, F, or K."),
        ],
        to_unit: Annotated[
            str,
            Field(description="Target unit of the same type, e.g. ft, g, or C."),
        ],
    ) -> dict[str, Any]:
        """Convert a value between length, mass, or temperature units."""
        return toolbox.unit_convert(value, from_unit, to_unit)

    @mcp.tool(
        title="Text statistics",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    def text_stats(
        text: Annotated[str, Field(description="Text to measure.")],
    ) -> dict[str, Any]:
        """Count characters, words, lines, and sentences in text."""
        return toolbox.text_stats(text)

    @mcp.tool(
        title="Preview CSV",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    async def csv_preview(
        path: Annotated[
            str,
            Field(description="CSV path relative to the workspace root."),
        ],
        ctx: Context,
        rows: Annotated[
            int,
            Field(description="Maximum data rows to return.", ge=1, le=MAX_CSV_ROWS),
        ] = 10,
    ) -> dict[str, Any]:
        """Preview a CSV file from the sandboxed workspace."""
        await ctx.report_progress(0, total=3, message=f"Opening {path}")
        await _server_log(ctx, "info", {"event": "csv_preview.start", "path": path})
        result = await asyncio.to_thread(toolbox.csv_preview, path, rows)
        await ctx.report_progress(3, total=3, message="CSV preview ready")
        await _server_log(
            ctx,
            "info",
            {"event": "csv_preview.done", "path": path, "rows": len(result["rows"])},
        )
        return result

    @mcp.tool(
        title="Query SQLite",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    @instrumented
    async def sqlite_query(
        path: Annotated[
            str,
            Field(description="SQLite database path relative to the workspace root."),
        ],
        sql: Annotated[
            str,
            Field(description="One read-only SELECT or WITH query."),
        ],
        ctx: Context,
        limit: Annotated[
            int,
            Field(description="Maximum rows to return.", ge=1, le=MAX_SQLITE_ROWS),
        ] = 50,
    ) -> dict[str, Any]:
        """Run a read-only query against a workspace SQLite database."""
        await ctx.report_progress(0, total=3, message=f"Querying {path}")
        await _server_log(ctx, "info", {"event": "sqlite_query.start", "path": path})
        result = await asyncio.to_thread(toolbox.sqlite_query, path, sql, limit)
        await ctx.report_progress(3, total=3, message="Query complete")
        await _server_log(
            ctx,
            "info",
            {"event": "sqlite_query.done", "path": path, "rows": len(result["rows"])},
        )
        return result

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
    async def write_file(
        path: Annotated[str, Field(description="File path relative to the workspace root.")],
        content: Annotated[str, Field(description="Full text content to write.")],
        ctx: Context,
    ) -> str:
        """Write a UTF-8 text file inside the sandboxed workspace."""
        if toolbox.settings.require_write_approval:
            capabilities = ctx.client_capabilities
            if capabilities is None or getattr(capabilities, "elicitation", None) is None:
                raise ToolError(
                    "This server requires write approval, but the client did not advertise "
                    "elicitation support. Re-run with --auto-approve-writes or set "
                    "REACT_MCP_REQUIRE_WRITE_APPROVAL=false to skip approval."
                )
            await _server_log(
                ctx,
                "info",
                {"event": "write.approval_requested", "path": path, "chars": len(content)},
            )
            approval = await ctx.elicit(
                f"Allow writing {len(content)} characters to {path!r}?",
                WriteApproval,
            )
            if approval.action == "decline":
                raise ToolError("Write declined by user; no file was written.")
            if approval.action == "cancel":
                raise ToolError("Write cancelled by user; no file was written.")
            if approval.data is None or not approval.data.approve:
                raise ToolError("Write not approved; no file was written.")
        await ctx.report_progress(0, total=2, message=f"Writing {path}")
        result = await asyncio.to_thread(toolbox.write_file, path, content)
        await ctx.report_progress(2, total=2, message="Write complete")
        await _server_log(
            ctx,
            "info",
            {"event": "write.completed", "path": path, "chars": len(content)},
        )
        return result


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

    @mcp.resource("workspace://index", name="workspace-index", mime_type="application/json")
    def workspace_index() -> str:
        """JSON index of every file in the sandboxed workspace."""
        return json.dumps(toolbox.workspace_index(), indent=2)

    @mcp.resource(
        "workspace://file/{+path}",
        name="workspace-file",
        mime_type="text/plain",
    )
    def workspace_file(path: str) -> str:
        """Read a workspace file by its relative path."""
        return toolbox.read_file(path, 20_000)


def _complete(values: list[str], prefix: str, limit: int = 50) -> Completion:
    lowered = prefix.lower()
    matches = [value for value in values if lowered in value.lower()]
    return Completion(
        values=matches[:limit],
        total=len(matches),
        has_more=len(matches) > limit,
    )


def register_completions(mcp: MCPServer, toolbox: Toolbox) -> None:
    @mcp.completion()
    async def complete(
        ref: ResourceTemplateReference | PromptReference,
        argument: Any,
        _context: Any,
    ) -> Completion | None:
        name = getattr(argument, "name", None)
        value = str(getattr(argument, "value", "") or "")
        notes = toolbox.note_index()
        if isinstance(ref, PromptReference) and ref.name == "research" and name == "topic":
            return _complete([item["title"] for item in notes], value)
        if isinstance(ref, ResourceTemplateReference):
            if ref.uri == "notes://{name}" and name == "name":
                return _complete([item["name"] for item in notes], value)
            if ref.uri == "workspace://file/{+path}" and name == "path":
                paths = [str(entry["path"]) for entry in toolbox.workspace_index()]
                return _complete(paths, value)
        return None


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
