"""Per-turn MCP client (AG-1): the agent's only path to banking data.

A fresh Streamable HTTP connection is opened per turn with the session token bound in the
Authorization header and the current trace context in the connect headers (OB-2). The token is
never a tool argument and never reaches the model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import httpx
from bank_shared.config import Settings
from bank_shared.errors import UpstreamUnavailableError
from bank_shared.tracing import inject_trace_headers, tracer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_tracer = tracer("agent_service.mcp")


def make_http_client(headers: dict[str, str], timeout: float) -> httpx.AsyncClient:
    """Factory for the transport's HTTP client; tests swap it for an in-process ASGI one."""
    return httpx.AsyncClient(headers=headers, timeout=timeout)


@dataclass
class ToolOutcome:
    name: str
    arguments: dict[str, Any]
    duration_ms: int
    ok: bool
    text: str
    structured: dict[str, Any] | None
    ui_resource: str | None = None


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    ui_resource: str | None = None


@dataclass
class McpTurnClient:
    """Async context manager valid for one /chat turn."""

    config: Settings
    session_token: str
    _session: ClientSession | None = None
    _stack: list[Any] = field(default_factory=list)
    tools: dict[str, ToolInfo] = field(default_factory=dict)

    async def __aenter__(self) -> McpTurnClient:
        headers = inject_trace_headers({"Authorization": f"Bearer {self.session_token}"})
        http = make_http_client(headers, self.config.llm_timeout_s)
        try:
            transport_cm = streamable_http_client(self.config.mcp_url, http_client=http)
            read_stream, write_stream, *_ = await transport_cm.__aenter__()
            self._stack.append(transport_cm)
            session_cm = ClientSession(read_stream, write_stream)
            self._session = await session_cm.__aenter__()
            self._stack.append(session_cm)
            await self._session.initialize()
            listed = await self._session.list_tools()
        except Exception as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise UpstreamUnavailableError("The banking tools are not reachable.") from error
        for tool in listed.tools:
            ui_meta = (tool.meta or {}).get("ui") or {}
            self.tools[tool.name] = ToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema or {"type": "object", "properties": {}},
                ui_resource=ui_meta.get("resourceUri"),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        while self._stack:
            cm = self._stack.pop()
            try:
                await cm.__aexit__(exc_type, exc, tb)
            except Exception:  # noqa: BLE001 - teardown must not mask the turn's outcome
                pass

    def ollama_tools(self) -> list[dict[str, Any]]:
        """MCP tool schemas in the chat-tools format the model expects (AG-4/AG-1)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": info.name,
                    "description": info.description,
                    "parameters": info.input_schema,
                },
            }
            for info in self.tools.values()
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        assert self._session is not None
        started = time.perf_counter()
        with _tracer.start_as_current_span(f"agent.tool.{name}"):
            try:
                result = await self._session.call_tool(name, arguments)
            except Exception as error:
                raise UpstreamUnavailableError("The banking tools are not reachable.") from error
        duration_ms = round((time.perf_counter() - started) * 1000)
        text = " ".join(
            block.text for block in result.content if getattr(block, "text", None)
        ).strip()
        info = self.tools.get(name)
        return ToolOutcome(
            name=name,
            arguments=arguments,
            duration_ms=duration_ms,
            ok=not result.is_error,
            text=text,
            structured=result.structured_content,
            ui_resource=info.ui_resource if info else None,
        )

    async def read_view(self, uri: str) -> tuple[str, str]:
        """(mime_type, html) of a ui:// resource, for embedding in the /chat response."""
        assert self._session is not None
        result = await self._session.read_resource(uri)
        content = result.contents[0]
        return content.mime_type or "text/html;profile=mcp-app", content.text or ""
