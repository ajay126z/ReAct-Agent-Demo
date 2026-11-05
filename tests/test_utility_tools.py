from __future__ import annotations

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from react_mcp.tools import Toolbox


def test_date_diff_days_and_hours(settings) -> None:
    box = Toolbox(settings)

    days = box.date_diff("2026-01-01", "2026-02-01", "days")
    assert days["value"] == 31
    assert days["unit"] == "days"

    hours = box.date_diff(
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T06:30:00+00:00",
        "hours",
    )
    assert hours["value"] == 30.5
    assert hours["unit"] == "hours"


def test_date_diff_rejects_mixed_timezones_and_bad_unit(settings) -> None:
    box = Toolbox(settings)
    with pytest.raises(ToolError, match="both include a timezone"):
        box.date_diff("2026-01-01", "2026-01-02T00:00:00+00:00", "days")
    with pytest.raises(ToolError, match="unit must be"):
        box.date_diff("2026-01-01", "2026-01-02", "weeks")


def test_add_days_preserves_date_and_datetime(settings) -> None:
    box = Toolbox(settings)
    assert box.add_days("2026-01-31", 1) == "2026-02-01"
    assert box.add_days("2026-01-31T23:00:00+00:00", 2) == "2026-02-02T23:00:00+00:00"


def test_unit_convert_length_mass_and_temperature(settings) -> None:
    box = Toolbox(settings)

    feet = box.unit_convert(1, "m", "ft")
    assert feet["unit"] == "ft"
    assert feet["value"] == pytest.approx(3.28083989501)

    kilograms = box.unit_convert(1, "lb", "kg")
    assert kilograms["value"] == pytest.approx(0.45359237)

    fahrenheit = box.unit_convert(100, "celsius", "fahrenheit")
    assert fahrenheit["value"] == 212

    kelvin = box.unit_convert(32, "F", "K")
    assert kelvin["value"] == pytest.approx(273.15)


def test_unit_convert_rejects_unknown_and_incompatible_units(settings) -> None:
    box = Toolbox(settings)
    with pytest.raises(ToolError, match="Unknown unit"):
        box.unit_convert(1, "m", "parsecs")
    with pytest.raises(ToolError, match="different unit types"):
        box.unit_convert(1, "m", "kg")


def test_text_stats(settings) -> None:
    stats = Toolbox(settings).text_stats("Hello world.\nSecond line!")
    assert stats == {
        "characters": 25,
        "characters_no_whitespace": 22,
        "words": 4,
        "lines": 2,
        "sentences": 2,
    }


def test_csv_preview_returns_columns_rows_and_truncation(settings) -> None:
    box = Toolbox(settings)
    box.write_file("scores.csv", "name,score\nAda,10\nGrace,20\n")

    preview = box.csv_preview("scores.csv", rows=1)
    assert preview["columns"] == ["name", "score"]
    assert preview["rows"] == [["Ada", "10"]]
    assert preview["truncated"] is True


def test_csv_preview_handles_empty_file(settings) -> None:
    box = Toolbox(settings)
    box.write_file("empty.csv", "")

    preview = box.csv_preview("empty.csv", rows=10)
    assert preview["columns"] == []
    assert preview["rows"] == []
    assert preview["truncated"] is False


def test_csv_preview_rejects_path_traversal(settings) -> None:
    with pytest.raises(ToolError, match="escapes"):
        Toolbox(settings).csv_preview("../outside.csv", rows=1)


def _make_database(settings) -> None:
    with sqlite3.connect(settings.workspace_dir / "data.db") as connection:
        connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany(
            "INSERT INTO items (name) VALUES (?)",
            [("alpha",), ("beta",), ("gamma",)],
        )


def test_sqlite_query_returns_rows_and_truncation(settings) -> None:
    box = Toolbox(settings)
    _make_database(settings)

    result = box.sqlite_query(
        "data.db",
        "SELECT id, name FROM items ORDER BY id",
        limit=2,
    )
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[1, "alpha"], [2, "beta"]]
    assert result["truncated"] is True

    cte = box.sqlite_query(
        "data.db",
        "WITH picked AS (SELECT 1 AS value) SELECT value FROM picked",
        limit=10,
    )
    assert cte["rows"] == [[1]]


def test_sqlite_query_converts_blobs_to_hex(settings) -> None:
    box = Toolbox(settings)
    with sqlite3.connect(settings.workspace_dir / "data.db") as connection:
        connection.execute("CREATE TABLE blobs (value BLOB)")
        connection.execute("INSERT INTO blobs VALUES (?)", (b"\x00\xff",))

    result = box.sqlite_query("data.db", "SELECT value FROM blobs", limit=1)
    assert result["rows"] == [["00ff"]]


def test_sqlite_query_rejects_writes_and_multiple_statements(settings) -> None:
    box = Toolbox(settings)
    _make_database(settings)

    with pytest.raises(ToolError, match="read-only"):
        box.sqlite_query("data.db", "DELETE FROM items", limit=10)
    with pytest.raises(ToolError, match="SQLite query failed"):
        box.sqlite_query(
            "data.db",
            "WITH doomed AS (SELECT 1) DELETE FROM items",
            limit=10,
        )
    with pytest.raises(ToolError, match="SQLite query failed"):
        box.sqlite_query("data.db", "SELECT 1; SELECT 2", limit=10)


def test_sqlite_query_rejects_missing_database_and_traversal(settings) -> None:
    box = Toolbox(settings)
    with pytest.raises(ToolError, match="does not exist"):
        box.sqlite_query("missing.db", "SELECT 1", limit=10)
    with pytest.raises(ToolError, match="escapes"):
        box.sqlite_query("../outside.db", "SELECT 1", limit=10)
