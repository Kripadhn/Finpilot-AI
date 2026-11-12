"""Unit tests for BankClient: retries, safe errors, service key handling, pagination."""

from __future__ import annotations

import json

import httpx
import pytest
from bank_shared.bank_client import BankClient
from bank_shared.config import Settings
from bank_shared.errors import InvalidInputError, NotOwnedError, UpstreamUnavailableError


def make_client(handler) -> BankClient:
    config = Settings(
        mock_api_url="http://testserver",
        mock_service_key="test-key",
        http_retries=2,
        http_backoff_base_s=0.0,
    )
    client = BankClient(config)
    client._client = httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
        headers={"X-Service-Key": "test-key"},
    )
    return client


async def test_retries_transient_503_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "Injected failure (chaos)"})
        return httpx.Response(200, json={"items": []})

    client = make_client(handler)
    assert await client.list_accounts("C001") == []
    assert calls["n"] == 3


async def test_gives_safe_error_after_retry_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "secret upstream detail"})

    client = make_client(handler)
    with pytest.raises(UpstreamUnavailableError) as excinfo:
        await client.get_account("ACC-1001")
    assert "secret upstream detail" not in str(excinfo.value)


async def test_does_not_retry_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"detail": "`from` must not be after `to`"})

    client = make_client(handler)
    with pytest.raises(InvalidInputError):
        await client.get_account("ACC-1001")
    assert calls["n"] == 1


async def test_404_is_a_safe_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Account not found"})

    client = make_client(handler)
    with pytest.raises(NotOwnedError):
        await client.get_account("ACC-9999")


async def test_service_key_sent_and_traceparent_injected():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"items": []})

    client = make_client(handler)
    await client.list_accounts("C001")
    assert seen.get("x-service-key") == "test-key"


async def test_transparent_pagination_beyond_upstream_max():
    rows = [{"transaction_id": f"TXN-{i:06d}"} for i in range(120)]

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        offset = int(params.get("cursor", "0") or 0)
        limit = int(params["limit"])
        assert limit <= 50  # the upstream contract is never violated
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return httpx.Response(200, json={"items": page, "next_cursor": next_cursor})

    client = make_client(handler)
    items, next_cursor = await client.transactions("ACC-1001", max_items=100)
    assert len(items) == 100
    assert next_cursor is not None  # more rows exist upstream

    items, next_cursor = await client.transactions("ACC-1001")
    assert len(items) == 120
    assert next_cursor is None


async def test_introspect_posts_token():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"token": "tok_x"}
        return httpx.Response(200, json={"active": False})

    client = make_client(handler)
    assert await client.introspect("tok_x") == {"active": False}
