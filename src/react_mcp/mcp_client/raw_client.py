"""A minimal MCP client written directly against JSON-RPC 2.0.

This deliberately does not use the SDK. It exists to make the wire visible:

- newline-delimited JSON over the server's stdin/stdout (the stdio transport),
- the ``initialize`` handshake used by MCP versions through 2025-11-25,
- ``notifications/initialized``, ``tools/list`` and ``tools/call``.

The SDK's default client negotiates the newer ``server/discover`` flow; the
server serves both eras. The legacy handshake is easier to read and is what most
MCP material still documents, so it is the one this client implements.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, Settings
from ..observability.logging import get_logger
from ..observability.tracer import Tracer
from .base import MCPClientError, ToolResult, ToolSpec, content_blocks_to_text

log = get_logger("mcp.raw")


class MCPRPCError(MCPClientError):
    """A JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        suffix = f" data={data!r}" if data is not None else ""
        super().__init__(f"JSON-RPC error {code}: {message}{suffix}")


def encode_frame(message: dict[str, Any]) -> bytes:
    """Serialize one JSON-RPC message as a newline-terminated stdio frame."""
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: bytes | str) -> dict[str, Any]:
    """Parse one newline-delimited JSON-RPC frame."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise MCPClientError(f"Expected a JSON object, got {type(message).__name__}")
    return message


class RawMCPClient:
    """Hand-rolled MCP stdio client with full frame logging."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        tracer: Tracer | None = None,
        echo: bool = True,
        command: list[str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.tracer = tracer
        self.echo = echo
        self.command = command or [sys.executable, "-m", "react_mcp.server"]
        self.timeout = timeout or self.settings.request_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._next_id = 1

        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str | None = None

    # -- lifecycle --------------------------------------------------------
    async def __aenter__(self) -> RawMCPClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        env = {**os.environ, **self.settings.server_env()}
        log.debug("starting server: %s", " ".join(self.command))
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(PROJECT_ROOT),
            limit=4 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="raw-mcp-reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="raw-mcp-stderr")

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._proc = None

        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPClientError("MCP server process closed"))
        self._pending.clear()

    # -- io ---------------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            line = await stdout.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = decode_frame(stripped)
            except (json.JSONDecodeError, MCPClientError) as exc:
                log.warning("unparseable server frame: %s", exc)
                self._record("recv", {"raw": stripped.decode("utf-8", "replace")})
                continue
            self._record("recv", message)

            message_id = message.get("id")
            if message_id is not None and message_id in self._pending:
                future = self._pending[message_id]
                if not future.done():
                    future.set_result(message)
            elif "method" in message:
                self._notifications.append(message)
                log.debug("server notification: %s", message.get("method"))
            else:
                log.debug("unmatched server frame: %s", message)

        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPClientError("MCP server closed the connection"))
        self._pending.clear()

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            log.debug("server stderr: %s", line.decode("utf-8", "replace").rstrip())

    def _record(self, direction: str, message: dict[str, Any]) -> None:
        if self.tracer is not None:
            self.tracer.protocol(direction, message, echo=self.echo)
        elif self.echo:
            from ..observability.render import print_protocol_frame

            print_protocol_frame(direction, message)

    async def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPClientError("MCP server is not running")
        self._record("send", message)
        self._proc.stdin.write(encode_frame(message))
        await self._proc.stdin.drain()

    # -- JSON-RPC ---------------------------------------------------------
    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc is None:
            raise MCPClientError("MCP server is not running")
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(message)
            response = await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError as exc:
            raise MCPClientError(f"Timed out waiting for {method!r} after {self.timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"] or {}
            raise MCPRPCError(
                int(error.get("code", -32603)),
                str(error.get("message", "Unknown error")),
                error.get("data"),
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    # -- MCP verbs --------------------------------------------------------
    async def initialize(self, *, client_name: str = "react-mcp-raw-client") -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "0.1.0"},
            },
        )
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = result.get("instructions")
        await self.notify("notifications/initialized")
        return result

    async def list_tools_raw(self) -> dict[str, Any]:
        return await self.request("tools/list", {})

    async def call_tool_raw(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def list_resources_raw(self) -> dict[str, Any]:
        return await self.request("resources/list", {})

    async def read_resource_raw(self, uri: str) -> dict[str, Any]:
        return await self.request("resources/read", {"uri": uri})

    async def list_prompts_raw(self) -> dict[str, Any]:
        return await self.request("prompts/list", {})

    async def get_prompt_raw(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return await self.request("prompts/get", {"name": name, "arguments": arguments or {}})

    # -- normalized interface (same as SDKMCPClient) ----------------------
    async def list_tools(self) -> list[ToolSpec]:
        result = await self.list_tools_raw()
        return [
            ToolSpec(
                name=tool.get("name", ""),
                title=tool.get("title"),
                description=tool.get("description", ""),
                input_schema=tool.get("inputSchema", tool.get("input_schema", {})),
            )
            for tool in result.get("tools", [])
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = await self.call_tool_raw(name, arguments)
        content = result.get("content") or []
        text = content_blocks_to_text(content)
        return ToolResult(
            content=text,
            is_error=bool(result.get("isError", result.get("is_error", False))),
            structured=result.get("structuredContent", result.get("structured_content")),
        )

    def drain_notifications(self) -> list[dict[str, Any]]:
        notifications = self._notifications
        self._notifications = []
        return notifications


def server_path() -> Path:
    return PROJECT_ROOT / "src" / "react_mcp" / "server.py"
