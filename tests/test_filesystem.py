from __future__ import annotations

import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from react_mcp.tools import Toolbox


def test_write_read_list_roundtrip(settings) -> None:
    box = Toolbox(settings)
    box.write_file("sub/hello.txt", "hello world")
    assert box.read_file("sub/hello.txt", max_bytes=100) == "hello world"
    entries = box.list_files("")
    paths = {entry["path"] for entry in entries}
    assert "sub/hello.txt" in paths
    assert "sub" in paths


def test_read_file_truncates(settings) -> None:
    box = Toolbox(settings)
    box.write_file("big.txt", "x" * 50)
    text = box.read_file("big.txt", max_bytes=10)
    assert text.startswith("x" * 10)
    assert "truncated at 10 bytes" in text


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "a/../../escape.txt"])
def test_absolute_and_traversal_paths_are_rejected(settings, path: str) -> None:
    box = Toolbox(settings)
    with pytest.raises(ToolError):
        box.write_file(path, "nope")
    with pytest.raises(ToolError):
        box.read_file(path, max_bytes=10)


def test_symlink_escape_is_rejected(settings) -> None:
    outside = settings.workspace_dir.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = settings.workspace_dir / "link.txt"
    os.symlink(outside, link)
    with pytest.raises(ToolError, match="escapes"):
        Toolbox(settings).read_file("link.txt", max_bytes=100)


def test_notes_search_and_read(settings) -> None:
    box = Toolbox(settings)
    results = box.search_notes("react observations", limit=5)
    assert results
    assert results[0]["name"] == "alpha"
    assert "ReAct" in box.read_note("alpha")
    assert "ReAct" in box.read_note("alpha.md")


def test_notes_reject_unknown_and_traversal(settings) -> None:
    box = Toolbox(settings)
    with pytest.raises(ToolError, match="No note named"):
        box.read_note("missing")
    with pytest.raises(ToolError, match="Invalid note name"):
        box.read_note("../alpha")
