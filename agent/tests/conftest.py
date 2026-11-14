"""Agent test fixtures: full in-process chain mock API <- MCP server <- agent loop.

No network, no model: the LLM is a scripted FakeProvider, the MCP server runs as an ASGI app
wired to the in-process mock API, and the agent's transport factory is patched to reach it.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

_tmp = Path(tempfile.mkdtemp(prefix="agent-tests-"))
os.environ.setdefault("MOCK_SERVICE_KEY", "test-service-key")
os.environ.setdefault("MOCK_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("FIXED_TODAY", "2026-10-31")
os.environ.setdefault("BANK_DB_PATH", str(_tmp / "bank.db"))
DB_PATH = Path(os.environ["BANK_DB_PATH"])

from seed.generate import generate  # noqa: E402

if not DB_PATH.exists():
    generate(DB_PATH)

from asgi_lifespan import LifespanManager  # noqa: E402
from bank_shared.bank_client import BankClient  # noqa: E402
from bank_shared.config import Settings  # noqa: E402
from bank_shared.tracing import setup_tracing  # noqa: E402
from compute_truth import build_truth, load_rows  # noqa: E402

setup_tracing("agent-tests")  # real span ids (no exporter), so trace_id flows in tests

import agent_service.mcp_client as mcp_client_module  # noqa: E402
from agent_service.llm import LLMReply, ToolCall  # noqa: E402
from agent_service.loop import AgentLoop  # noqa: E402
from agent_service.memory import ConversationStore  # noqa: E402

SERVICE_KEY = os.environ["MOCK_SERVICE_KEY"]


class FakeProvider:
    """Scripted LLM. Each run_turn pops the next reply; an empty script means 'no tools'."""

    def __init__(self) -> None:
        self.replies: list[LLMReply] = []
        self.calls = 0

    def script(self, *tool_calls: tuple[str, dict], content: str = "") -> None:
        self.replies.append(
            LLMReply(
                content=content,
                tool_calls=[ToolCall(name=name, arguments=args) for name, args in tool_calls],
                input_tokens=10,
                output_tokens=5,
                model="fake",
            )
        )

    async def chat(self, messages, tools) -> LLMReply:
        self.calls += 1
        if self.replies:
            return self.replies.pop(0)
        return LLMReply(content="", tool_calls=[], model="fake")

    async def aclose(self) -> None:
        return None


@pytest.fixture(scope="session")
def truth() -> dict:
    return build_truth(*load_rows(DB_PATH))


@pytest.fixture(scope="session")
def mock_app():
    from app.main import app

    return app


@pytest.fixture
def test_config() -> Settings:
    return Settings(
        mock_api_url="http://127.0.0.1:8001",
        mock_service_key=SERVICE_KEY,
        mcp_url="http://127.0.0.1:8002/mcp",
        fixed_today=date(2026, 10, 31),
        http_retries=1,
        http_backoff_base_s=0.0,
        llm_provider="ollama",
        expose_safe_logs=True,
    )


@pytest_asyncio.fixture
async def mcp_app(mock_app, monkeypatch):
    """The real MCP server app, its service wired to the in-process mock API."""
    import mcp_bank_server.server as server_module
    from mcp_bank_server.service import BankService

    config = Settings(
        mock_api_url="http://127.0.0.1:8001",
        mock_service_key=SERVICE_KEY,
        http_retries=1,
        http_backoff_base_s=0.0,
    )
    client = BankClient(config)
    client._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8001",
        transport=httpx.ASGITransport(app=mock_app),
        headers={"X-Service-Key": SERVICE_KEY},
    )
    monkeypatch.setattr(server_module, "service", BankService(client))
    app = server_module.mcp.streamable_http_app(stateless_http=True, json_response=True)
    async with LifespanManager(app):
        yield app


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def agent(mcp_app, provider, test_config, monkeypatch) -> AgentLoop:
    def asgi_http_client(headers: dict[str, str], timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_app),
            base_url="http://127.0.0.1:8002",
            headers=headers,
            timeout=timeout,
        )

    monkeypatch.setattr(mcp_client_module, "make_http_client", asgi_http_client)
    store = ConversationStore(max_conversations=50, ttl_s=3600, max_history_turns=12)
    return AgentLoop(test_config, provider, store)
