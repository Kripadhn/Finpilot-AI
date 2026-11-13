"""MCP protocol tests over the real Streamable HTTP app (MC-1, MC-2, MC-3, MC-7, MC-8).

Runs the actual Starlette app in-process (ASGI) and talks to it with the official MCP client,
with the session token in the Authorization header — exactly like the agent service does.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import mcp_bank_server.server as server_module
import pytest
from asgi_lifespan import LifespanManager
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_bank_server.service import BankService

from .conftest import make_bank_client


@pytest.fixture
def wired_server(mock_app, monkeypatch):
    """Point the MCP server's service at the in-process mock API."""
    monkeypatch.setattr(
        server_module, "service", BankService(make_bank_client(httpx.ASGITransport(app=mock_app)))
    )
    return server_module.mcp


@asynccontextmanager
async def mcp_session(wired_server, token: str | None):
    app = wired_server.streamable_http_app(stateless_http=True, json_response=True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with LifespanManager(app):
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8002",
            headers=headers,
        )
        async with streamable_http_client("http://127.0.0.1:8002/mcp", http_client=http) as (
            read_stream,
            write_stream,
            *_,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def test_tools_schemas_and_ui_metadata(wired_server):
    async with mcp_session(wired_server, "tok_aisha_valid") as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {
            "list_accounts",
            "get_balance",
            "get_transactions",
            "spending_summary",
        }
        for tool in tools.values():
            assert tool.description
            properties = (tool.input_schema or {}).get("properties", {})
            assert "customer_id" not in properties  # MC-3
            assert "token" not in properties and "session_token" not in properties
        # MCP Apps 2026-01-26 linkage on the view-bearing tools
        assert tools["get_transactions"].meta["ui"]["resourceUri"] == "ui://bank/transactions"
        assert (
            tools["spending_summary"].meta["ui"]["resourceUri"] == "ui://bank/spending-by-category"
        )


async def test_call_tool_returns_structured_and_text(wired_server):
    async with mcp_session(wired_server, "tok_aisha_valid") as session:
        result = await session.call_tool("list_accounts", {})
        assert not result.is_error
        assert result.structured_content["count"] == 2
        assert result.content[0].type == "text" and result.content[0].text  # MC-7


async def test_expired_session_is_rejected_with_no_data(wired_server):
    async with mcp_session(wired_server, "tok_aisha_expired") as session:
        result = await session.call_tool("list_accounts", {})
        assert result.is_error
        text = " ".join(block.text for block in result.content if block.type == "text")
        assert "session is not valid" in text
        assert "ACC-" not in text and result.structured_content is None


async def test_missing_token_is_rejected(wired_server):
    async with mcp_session(wired_server, None) as session:
        result = await session.call_tool("get_balance", {"account_id": "ACC-1001"})
        assert result.is_error


async def test_foreign_account_is_rejected_over_mcp(wired_server):
    async with mcp_session(wired_server, "tok_aisha_valid") as session:
        result = await session.call_tool("get_balance", {"account_id": "ACC-2001"})
        assert result.is_error
        text = " ".join(block.text for block in result.content if block.type == "text")
        assert "No such account" in text
        assert "Omar" not in text and "C002" not in text


async def test_resources_and_prompt(wired_server):
    async with mcp_session(wired_server, "tok_aisha_valid") as session:
        categories = await session.read_resource("bank://categories")
        payload = json.loads(categories.contents[0].text)
        assert "groceries" in payload["categories"] and len(payload["categories"]) == 13

        for uri in ("ui://bank/transactions", "ui://bank/spending-by-category"):
            view = await session.read_resource(uri)
            content = view.contents[0]
            assert content.mime_type == "text/html;profile=mcp-app"
            assert "<html" in content.text and "ui/notifications/tool-result" in content.text

        prompt = await session.get_prompt("monthly-summary", {"month": "2026-09"})
        assert "2026-09" in prompt.messages[0].content.text
