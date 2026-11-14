"""Agent service: POST /chat on :8003 implementing the contract in README 6.5 (AG-4)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from bank_shared.config import settings
from bank_shared.errors import SafeError
from bank_shared.logging import captured_logs, setup_logging
from bank_shared.tracing import setup_tracing
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .llm import make_provider
from .loop import AgentLoop
from .mcp_client import McpTurnClient
from .memory import ConversationStore


class ChatRequest(BaseModel):
    session_token: str = Field(min_length=1)
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


class UiToolRequest(BaseModel):
    """Host-bridge tool call from a view (UI-3). Same MCP path, same server-side isolation."""

    session_token: str = Field(min_length=1)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


_ALLOWED_UI_TOOLS = {"get_transactions", "spending_summary"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings()
    setup_logging("agent", secrets=[config.mock_service_key, config.mock_admin_key])
    setup_tracing("bank-agent")
    provider = make_provider(config)
    store = ConversationStore(
        max_conversations=config.max_conversations,
        ttl_s=config.conversation_ttl_s,
        max_history_turns=config.max_history_turns,
    )
    app.state.config = config
    app.state.loop = AgentLoop(config, provider, store)
    yield
    await provider.aclose()


app = FastAPI(title="DVT Agentic Bank – Agent service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    response = await app.state.loop.run_turn(
        request.session_token, request.conversation_id, request.message
    )
    return {
        "conversation_id": response.conversation_id,
        "text": response.text,
        "tool_calls": response.tool_calls,
        "ui_resources": response.ui_resources,
        "structured": response.structured,
        "usage": response.usage,
        "trace_id": response.trace_id,
    }


@app.post("/ui/tool")
async def ui_tool(request: UiToolRequest) -> dict[str, Any]:
    """Executes a view-initiated tool call through MCP. Isolation stays server-side (SE-2)."""
    if request.name not in _ALLOWED_UI_TOOLS:
        return {"isError": True, "content": [{"type": "text", "text": "Tool not allowed."}]}
    try:
        async with McpTurnClient(app.state.config, request.session_token) as mcp:
            outcome = await mcp.call(request.name, request.arguments)
    except SafeError as error:
        return {"isError": True, "content": [{"type": "text", "text": error.message}]}
    return {
        "isError": not outcome.ok,
        "content": [{"type": "text", "text": outcome.text}],
        "structuredContent": outcome.structured,
    }


@app.get("/logs")
def safe_logs() -> dict[str, Any]:
    """Captured, redacted logs for the harness's no_leak check. Off unless EXPOSE_SAFE_LOGS."""
    if not settings().expose_safe_logs:
        return {"enabled": False, "lines": []}
    return {"enabled": True, "lines": captured_logs()}
