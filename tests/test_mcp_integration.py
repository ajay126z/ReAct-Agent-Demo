from __future__ import annotations

import json

import pytest

from react_mcp.mcp_client.raw_client import RawMCPClient
from react_mcp.mcp_client.sdk_client import SDKMCPClient

pytestmark = pytest.mark.integration


async def test_sdk_client_lists_calls_and_reads(settings) -> None:
    async with SDKMCPClient(settings) as client:
        assert client.server_name
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert {
            "calculator",
            "date_diff",
            "add_days",
            "unit_convert",
            "text_stats",
            "csv_preview",
            "sqlite_query",
            "search_notes",
            "read_note",
            "list_files",
        } <= names

        calculator = next(tool for tool in tools if tool.name == "calculator")
        assert "expression" in calculator.properties
        assert calculator.required == ["expression"]

        result = await client.call_tool("calculator", {"expression": "(17 * 3) + 9"})
        assert result.is_error is False
        assert result.content == "60"

        notes = json.loads(await client.read_resource("notes://index"))
        assert {note["name"] for note in notes} == {"alpha", "beta"}

        prompt = await client.get_prompt("explain_mcp")
        assert "MCP lifecycle" in prompt

        resources = await client.list_resources()
        assert "notes://index" in resources

        prompts = await client.list_prompts()
        assert "explain_mcp" in prompts


async def test_raw_client_legacy_initialize_and_call(settings) -> None:
    async with RawMCPClient(settings, echo=False) as client:
        init = await client.initialize()
        assert init["protocolVersion"] == "2025-11-25"
        assert client.protocol_version == "2025-11-25"
        assert client.server_info.get("name")

        tools = await client.list_tools_raw()
        assert any(tool["name"] == "calculator" for tool in tools["tools"])

        result = await client.call_tool_raw("calculator", {"expression": "2 ** 10"})
        assert result["content"][0]["text"] == "1024"
        assert result.get("isError") in (False, None)

        notes = await client.read_resource_raw("notes://index")
        assert "alpha" in notes["contents"][0]["text"]

        prompt = await client.get_prompt_raw("explain_mcp")
        assert prompt["messages"][0]["content"]["text"]
