"""Shared fixtures: in-process mock API (ASGI), BankService over it, and ground truth.

Mirrors mock-bank-api/tests/conftest.py: env vars are set (setdefault, so whichever conftest
loads first wins) before app.main is imported, and a deterministic temp DB is generated.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import httpx
import pytest

_tmp = Path(tempfile.mkdtemp(prefix="mcp-server-tests-"))
os.environ.setdefault("MOCK_SERVICE_KEY", "test-service-key")
os.environ.setdefault("MOCK_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("FIXED_TODAY", "2026-10-31")
os.environ.setdefault("BANK_DB_PATH", str(_tmp / "bank.db"))
DB_PATH = Path(os.environ["BANK_DB_PATH"])

from seed.generate import generate  # noqa: E402

if not DB_PATH.exists():
    generate(DB_PATH)

from bank_shared.bank_client import BankClient  # noqa: E402
from bank_shared.config import Settings  # noqa: E402
from compute_truth import build_truth, load_rows  # noqa: E402
from mcp_bank_server.service import BankService  # noqa: E402

SERVICE_KEY = os.environ["MOCK_SERVICE_KEY"]


def make_bank_client(transport: httpx.BaseTransport) -> BankClient:
    config = Settings(
        mock_api_url="http://127.0.0.1:8001",
        mock_service_key=SERVICE_KEY,
        http_retries=2,
        http_backoff_base_s=0.0,
    )
    client = BankClient(config)
    client._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8001",
        transport=transport,
        headers={"X-Service-Key": SERVICE_KEY},
    )
    return client


@pytest.fixture(scope="session")
def mock_app():
    from app.main import app

    return app


@pytest.fixture
def bank_client(mock_app) -> BankClient:
    return make_bank_client(httpx.ASGITransport(app=mock_app))


@pytest.fixture
def bank_service(bank_client) -> BankService:
    return BankService(bank_client)


@pytest.fixture(scope="session")
def truth() -> dict:
    return build_truth(*load_rows(DB_PATH))
