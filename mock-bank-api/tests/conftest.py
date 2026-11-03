import os
import tempfile
from pathlib import Path

import pytest

# app.main reads these at import, so set them before any test module imports it.
_tmp = Path(tempfile.mkdtemp(prefix="mock-bank-tests-"))
DB_PATH = _tmp / "bank.db"
os.environ["MOCK_SERVICE_KEY"] = "test-service-key"
os.environ["MOCK_ADMIN_KEY"] = "test-admin-key"
os.environ["FIXED_TODAY"] = "2026-10-31"
os.environ["BANK_DB_PATH"] = str(DB_PATH)

from seed.generate import generate  # noqa: E402

generate(DB_PATH)


@pytest.fixture(scope="session")
def db_path() -> Path:
    return DB_PATH


@pytest.fixture
def client():
    from app.main import app, chaos
    from fastapi.testclient import TestClient

    with TestClient(app, headers={"X-Service-Key": "test-service-key"}) as test_client:
        yield test_client
    chaos.error_rate = 0.0
    chaos.latency_ms = 0
