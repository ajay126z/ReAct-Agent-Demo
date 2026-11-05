from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp import types as mcp_types

from react_mcp.mcp_client.sdk_client import SDKMCPClient

pytestmark = pytest.mark.integration


async def test_resources_completions_and_clean_tool_schemas(settings) -> None:
    (settings.workspace_dir / "scores.csv").write_text("name,score\nalpha,10\n", encoding="utf-8")
    nested = settings.workspace_dir / "sub"
    nested.mkdir()
    (nested / "hello.txt").write_text("hello nested", encoding="utf-8")

    async with SDKMCPClient(settings) as client:
        tools = await client.list_tools()
        for name in ("csv_preview", "sqlite_query", "write_file"):
            tool = next(item for item in tools if item.name == name)
            assert "ctx" not in tool.properties
            assert "context" not in tool.properties

        templates = await client.list_resource_templates()
        assert "workspace://file/{+path}" in templates
        resources = await client.list_resources()
        assert "workspace://index" in resources

        index = json.loads(await client.read_resource("workspace://index"))
        assert {"path": "scores.csv", "type": "file", "size": 20} in index

        assert await client.read_resource("workspace://file/sub/hello.txt") == "hello nested"
        assert await client.complete_resource("notes://{name}", "name", "al") == ["alpha"]
        assert await client.complete_resource("workspace://file/{+path}", "path", "hello") == [
            "sub/hello.txt"
        ]
        assert await client.complete_prompt("research", "topic", "Beta") == ["Beta note"]


async def test_progress_and_server_logs(settings) -> None:
    (settings.workspace_dir / "scores.csv").write_text("name,score\nalpha,10\n", encoding="utf-8")
    progress: list[tuple[float, float | None, str | None]] = []
    logs: list[Any] = []

    async def on_progress(current: float, total: float | None, message: str | None) -> None:
        progress.append((current, total, message))

    async def on_log(params: Any) -> None:
        logs.append(params)

    async with SDKMCPClient(settings, logging_callback=on_log, log_level="info") as client:
        result = await client.call_tool(
            "csv_preview", {"path": "scores.csv"}, progress_callback=on_progress
        )
    await asyncio.sleep(0)

    assert result.is_error is False
    assert progress[0][0] == 0
    assert progress[-1][0] == 3
    assert any("csv_preview.start" in json.dumps(log.data) for log in logs)


async def test_write_requires_elicitation_approval(settings) -> None:
    async def approve(context: Any, params: Any) -> Any:
        return mcp_types.ElicitResult(action="accept", content={"approve": True})

    target = settings.workspace_dir / "approved.txt"
    async with SDKMCPClient(settings, elicitation_callback=approve) as client:
        result = await client.call_tool(
            "write_file", {"path": "approved.txt", "content": "approved"}
        )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "approved"


async def test_write_decline_and_missing_capability(settings) -> None:
    async def decline(context: Any, params: Any) -> Any:
        return mcp_types.ElicitResult(action="decline")

    target = settings.workspace_dir / "declined.txt"
    async with SDKMCPClient(settings, elicitation_callback=decline) as client:
        declined = await client.call_tool(
            "write_file", {"path": "declined.txt", "content": "blocked"}
        )
    assert declined.is_error is True
    assert "declined" in declined.content
    assert not target.exists()

    async with SDKMCPClient(settings) as client:
        missing = await client.call_tool(
            "write_file", {"path": "missing.txt", "content": "blocked"}
        )
    assert missing.is_error is True
    assert "elicitation" in missing.content
    assert not (settings.workspace_dir / "missing.txt").exists()


async def test_auto_approve_writes_skips_elicitation(settings) -> None:
    open_settings = settings.model_copy(update={"require_write_approval": False})
    target = settings.workspace_dir / "automatic.txt"

    async with SDKMCPClient(open_settings) as client:
        result = await client.call_tool(
            "write_file", {"path": "automatic.txt", "content": "automatic"}
        )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "automatic"
