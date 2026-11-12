"""Async client for the mock core banking API (MC-6).

- finite timeouts;
- bounded retry with exponential backoff + jitter for transient failures only (connect/transport
  errors and 5xx), never 4xx;
- X-Service-Key only from the environment, never logged;
- redacted structured logs (method, path, status, attempt, duration — never payloads);
- W3C traceparent injected into every request (OB-2);
- transparent pagination above the upstream per-request maximum of 50 (MC-5).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date
from typing import Any

import httpx

from .config import Settings, settings
from .errors import InvalidInputError, NotOwnedError, UpstreamUnavailableError
from .tracing import inject_trace_headers

logger = logging.getLogger("bank_shared.client")

UPSTREAM_PAGE_MAX = 50


class BankClient:
    """One instance per service process. `aclose()` on shutdown."""

    def __init__(self, config: Settings | None = None) -> None:
        self._config = config or settings()
        self._client = httpx.AsyncClient(
            base_url=self._config.mock_api_url,
            timeout=httpx.Timeout(
                self._config.http_timeout_s, connect=self._config.http_connect_timeout_s
            ),
            headers={"X-Service-Key": self._config.mock_service_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        attempts = self._config.http_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method, path, headers=inject_trace_headers({}), **kwargs
                )
            except httpx.HTTPError as error:
                last_error = error
                logger.warning(
                    "bank api transport error",
                    extra={"tool": path, "attempt": attempt, "status": type(error).__name__},
                )
            else:
                duration_ms = round((time.perf_counter() - started) * 1000)
                logger.info(
                    f"bank api {method} {path}",
                    extra={
                        "status": response.status_code,
                        "attempt": attempt,
                        "duration_ms": duration_ms,
                    },
                )
                if response.status_code < 500:
                    return response
                last_error = httpx.HTTPStatusError(
                    f"server error {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < attempts:
                backoff = self._config.http_backoff_base_s * (2 ** (attempt - 1))
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
        logger.error("bank api unavailable", extra={"tool": path})
        raise UpstreamUnavailableError() from last_error

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        if response.status_code == 404:
            # The caller decides how to phrase it; never forward the upstream body.
            raise NotOwnedError()
        if response.status_code == 400:
            raise InvalidInputError("The banking service rejected the request parameters.")
        if response.status_code >= 400:
            raise UpstreamUnavailableError()
        return response.json()

    async def introspect(self, token: str) -> dict[str, Any]:
        response = await self._request("POST", "/sessions/introspect", json={"token": token})
        if response.status_code >= 400:
            raise UpstreamUnavailableError()
        return response.json()

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        return await self._get_json(f"/customers/{customer_id}")

    async def list_accounts(self, customer_id: str) -> list[dict[str, Any]]:
        payload = await self._get_json(f"/customers/{customer_id}/accounts")
        return payload["items"]

    async def get_account(self, account_id: str) -> dict[str, Any]:
        return await self._get_json(f"/accounts/{account_id}")

    async def get_categories(self) -> list[str]:
        payload = await self._get_json("/categories")
        return payload["items"]

    async def transactions_page(
        self,
        account_id: str,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= UPSTREAM_PAGE_MAX:
            raise InvalidInputError(f"Upstream page size must be 1-{UPSTREAM_PAGE_MAX}.")
        params: dict[str, Any] = {"limit": limit}
        if from_date:
            params["from"] = from_date.isoformat()
        if to_date:
            params["to"] = to_date.isoformat()
        if category:
            params["category"] = category
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._get_json(f"/accounts/{account_id}/transactions", params=params)

    async def transactions(
        self,
        account_id: str,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        category: str | None = None,
        status: str | None = None,
        max_items: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch up to max_items (or everything) with transparent pagination (MC-5).

        Returns (items, next_cursor): next_cursor is non-None only when max_items was reached
        and more data exists upstream.
        """
        items: list[dict[str, Any]] = []
        while True:
            remaining = None if max_items is None else max_items - len(items)
            if remaining is not None and remaining <= 0:
                return items, cursor
            page_size = UPSTREAM_PAGE_MAX if remaining is None else min(remaining, 50)
            page = await self.transactions_page(
                account_id,
                from_date=from_date,
                to_date=to_date,
                category=category,
                status=status,
                limit=page_size,
                cursor=cursor,
            )
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                return items, None
