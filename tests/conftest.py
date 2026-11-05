from __future__ import annotations

from pathlib import Path

import pytest

from react_mcp.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    notes = tmp_path / "notes"
    workspace = tmp_path / "workspace"
    sessions = tmp_path / "sessions"
    traces = tmp_path / "traces"
    notes.mkdir()
    workspace.mkdir()
    sessions.mkdir()
    (notes / "alpha.md").write_text(
        "# Alpha note\n\nReAct alternates thoughts, actions, and observations.\n",
        encoding="utf-8",
    )
    (notes / "beta.md").write_text(
        "# Beta note\n\nMCP exposes tools, resources, and prompts over JSON-RPC.\n",
        encoding="utf-8",
    )
    return Settings(
        model="test-model",
        workspace_dir=workspace,
        notes_dir=notes,
        session_dir=sessions,
        trace_dir=traces,
        request_timeout=10,
        max_steps=4,
        max_parse_retries=1,
    )
