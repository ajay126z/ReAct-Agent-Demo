"""MCP client built on the official Python SDK."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from ..config import PROJECT_ROOT, Settings
from ..observability.logging import get_logger
from ..observability.tracer import Tracer
from .base import ToolResult, ToolSpec, content_blocks_to_text

log = get_logger("mcp.client")


class SDKMCPClient:
    """Thin, instrumented wrapper around ``mcp.Client``.

    Supports both transports:
    - ``stdio``: launches ``python -m react_mcp.server`` as a subprocess.
    - ``http``: connects to a Streamable HTTP endpoint (``react-mcp serve --http``).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        tracer: Tracer | None = None,
        transport: str | None = None,
        url: str | None = None,
    ) -> None:
        self.settings = settings
        self.tracer = tracer
        self.transport = transport or settings.transport
        self.url = url or settings.server_url
        self._client: Client | None = None
        self._cm: Any = None
        self.protocol_version: str | None = None
        self.server_name: str | None = None

    def _target(self) -> Any:
        if self.transport == "http":
            return self.url
        env = self.settings.server_env()
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "react_mcp.server"],
            env=env,
            cwd=str(PROJECT_ROOT),
        )

    async def __aenter__(self) -> SDKMCPClient:
        target = self._target()
        if self.tracer is not None:
            with self.tracer.span(
                "mcp.connect",
                kind="client",
                transport=self.transport,
                target=self.url if self.transport == "http" else "stdio://react_mcp.server",
            ):
                await self._connect(target)
        else:
            await self._connect(target)
        return self

    async def _connect(self, target: Any) -> None:
        self._cm = Client(
            target,
            read_timeout_seconds=self.settings.request_timeout,
            mode="auto",
        )
        self._client = await self._cm.__aenter__()
        self.protocol_version = self._client.protocol_version
        info = self._client.server_info
        self.server_name = info.name if info is not None else "unknown"
        log.info(
            "connected server=%s protocol=%s transport=%s",
            self.server_name,
            self.protocol_version,
            self.transport,
        )

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.tracer is not None:
            with self.tracer.span("mcp.disconnect", kind="client"):
                await self._disconnect()
        else:
            await self._disconnect()

    async def _disconnect(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
        self._cm = None
        self._client = None

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("MCP client is not connected; use 'async with SDKMCPClient(...)'.")
        return self._client

    async def list_tools(self) -> list[ToolSpec]:
        client = self._require_client()
        if self.tracer is not None:
            with self.tracer.span("mcp.tools/list", kind="client") as span:
                result = await client.list_tools()
                span.attributes["tool_count"] = len(result.tools)
        else:
            result = await client.list_tools()

        specs = [
            ToolSpec(
                name=tool.name,
                title=tool.title,
                description=tool.description or "",
                input_schema=tool.input_schema or {},
            )
            for tool in result.tools
        ]
        log.debug("tools listed count=%d", len(specs))
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        client = self._require_client()
        attributes = {
            "mcp.tool.name": name,
            "arguments": arguments,
        }
        if self.tracer is not None:
            with self.tracer.span("mcp.tools/call", kind="client", **attributes) as span:
                result = await client.call_tool(name, arguments)
                span.attributes["is_error"] = bool(result.is_error)
                span.attributes["content_blocks"] = len(result.content or [])
                self.tracer.event(
                    "mcp.tool.result",
                    tool=name,
                    is_error=bool(result.is_error),
                )
        else:
            result = await client.call_tool(name, arguments)

        text = content_blocks_to_text(list(result.content or []))
        return ToolResult(
            content=text,
            is_error=bool(result.is_error),
            structured=result.structured_content,
        )

    async def read_resource(self, uri: str) -> str:
        client = self._require_client()
        if self.tracer is not None:
            with self.tracer.span("mcp.resources/read", kind="client", uri=uri):
                result = await client.read_resource(uri)
        else:
            result = await client.read_resource(uri)
        return content_blocks_to_text(list(result.contents or []))

    async def list_resources(self) -> list[str]:
        client = self._require_client()
        if self.tracer is not None:
            with self.tracer.span("mcp.resources/list", kind="client"):
                result = await client.list_resources()
        else:
            result = await client.list_resources()
        return [str(resource.uri) for resource in result.resources]

    async def list_prompts(self) -> list[str]:
        client = self._require_client()
        if self.tracer is not None:
            with self.tracer.span("mcp.prompts/list", kind="client"):
                result = await client.list_prompts()
        else:
            result = await client.list_prompts()
        return [prompt.name for prompt in result.prompts]

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> str:
        client = self._require_client()
        if self.tracer is not None:
            with self.tracer.span("mcp.prompts/get", kind="client", prompt=name):
                result = await client.get_prompt(name, arguments)
        else:
            result = await client.get_prompt(name, arguments)
        return "\n".join(
            content_blocks_to_text([message.content]) for message in result.messages
        ).strip()


def default_server_command() -> list[str]:
    """Convenience for docs/tests: the stdio command the client launches."""
    return [sys.executable, "-m", "react_mcp.server"]


def project_relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
