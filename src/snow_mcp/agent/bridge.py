"""Bridge between an MCP server and the Anthropic Messages API.

Responsibilities:

1. connect to the MCP server (in-process, stdio subprocess, or HTTP)
2. translate ``tools/list`` output into the ``tools`` parameter of the
   Messages API — the JSON Schema is passed through unchanged, which is the
   whole point of MCP: the tool contract is authored once, on the server
3. execute ``tools/call`` and time every round trip

The latency recorded here is *MCP round-trip latency*: serialise, transport,
server-side handler, backend, response. It deliberately excludes model time so
the eval can attribute cost to the right layer.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class ToolCallRecord:
    """One MCP tool invocation, with everything the eval needs to grade it."""

    name: str
    arguments: dict[str, Any]
    latency_ms: float
    ok: bool
    result_text: str = ""
    error: str | None = None
    sequence: int = 0

    def truncated(self, limit: int = 400) -> str:
        text = self.result_text or ""
        return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "name": self.name,
            "arguments": self.arguments,
            "latency_ms": round(self.latency_ms, 2),
            "ok": self.ok,
            "error": self.error,
            "result_preview": self.truncated(240),
        }


class StdioTransport:
    """Adapts ``stdio_client`` to the ``Transport`` protocol expected by ``Client``."""

    def __init__(self, params: StdioServerParameters):
        self._params = params
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        self._cm = stdio_client(self._params)
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc_info: Any) -> Any:
        if self._cm is None:
            return False
        cm, self._cm = self._cm, None
        return await cm.__aexit__(*exc_info)


@dataclass
class MCPToolBridge:
    """Async context manager owning one MCP session."""

    target: Any
    """An ``MCPServer`` instance (in-process), a URL string, or ``StdioServerParameters``."""

    calls: list[ToolCallRecord] = field(default_factory=list)
    _client: Client | None = None
    _stack: AsyncExitStack | None = None
    _tools: list[dict[str, Any]] = field(default_factory=list)

    async def __aenter__(self) -> MCPToolBridge:
        self._stack = AsyncExitStack()
        target = self.target
        if isinstance(target, StdioServerParameters):
            target = StdioTransport(target)
        self._client = await self._stack.enter_async_context(Client(target))
        await self.refresh_tools()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    async def refresh_tools(self) -> list[dict[str, Any]]:
        """Fetch the server's tool catalogue as Anthropic tool specifications."""
        assert self._client is not None, "bridge is not connected"
        listing = await self._client.list_tools()
        self._tools = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
            }
            for tool in listing.tools
        ]
        return self._tools

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [tool["name"] for tool in self._tools]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCallRecord:
        """Invoke a tool and record its latency and outcome."""
        assert self._client is not None, "bridge is not connected"
        arguments = arguments or {}
        started = time.perf_counter()
        try:
            result = await self._client.call_tool(name, arguments)
            elapsed = (time.perf_counter() - started) * 1000.0
            text = "\n".join(
                block.text for block in (result.content or []) if getattr(block, "text", None)
            )
            # A server-side domain error is returned as JSON with an "error" key
            # (recoverable); a protocol error sets is_error.
            failed = bool(result.is_error) or '"error"' in text[:200]
            record = ToolCallRecord(
                name=name,
                arguments=arguments,
                latency_ms=elapsed,
                ok=not failed,
                result_text=text,
                error=text[:500] if failed else None,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            record = ToolCallRecord(
                name=name,
                arguments=arguments,
                latency_ms=elapsed,
                ok=False,
                result_text="",
                error=f"{type(exc).__name__}: {exc}",
            )
        record.sequence = len(self.calls) + 1
        self.calls.append(record)
        return record

    def reset_calls(self) -> None:
        self.calls = []


def stdio_target(command: str, args: Sequence[str], env: dict[str, str] | None = None) -> StdioServerParameters:
    """Build stdio parameters for launching the MCP server as a subprocess."""
    return StdioServerParameters(command=command, args=list(args), env=dict(env or {}))
