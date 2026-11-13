"""Service-layer tests against the in-process mock API.

Covers: ownership isolation, expired/unknown sessions, pagination, invalid input, refunds,
pending/reversed exclusion, retries/timeouts, Arabic payloads and the seeded
prompt-injection-bearing transaction (kept as data).
"""

from __future__ import annotations

import re

import httpx
import pytest
from bank_shared.errors import (
    InvalidInputError,
    NotOwnedError,
    SessionInvalidError,
    UpstreamUnavailableError,
)
from mcp_bank_server.service import BankService

from .conftest import make_bank_client

AISHA, OMAR, LINA = "tok_aisha_valid", "tok_omar_valid", "tok_lina_valid"


# --- identity and isolation (SE-1, SE-2, MC-3) ---


async def test_expired_token_gets_no_data(bank_service):
    with pytest.raises(SessionInvalidError):
        await bank_service.list_accounts("tok_aisha_expired")


async def test_unknown_and_missing_tokens_get_no_data(bank_service):
    with pytest.raises(SessionInvalidError):
        await bank_service.list_accounts("tok_nobody")
    with pytest.raises(SessionInvalidError):
        await bank_service.list_accounts(None)


async def test_missing_scope_is_rejected(bank_service, monkeypatch):
    async def introspect(token):
        return {"active": True, "customer_id": "C001", "scopes": ["accounts:read"]}

    monkeypatch.setattr(bank_service._client, "introspect", introspect)
    with pytest.raises(SessionInvalidError):
        await bank_service.get_transactions("tok_aisha_valid", "ACC-1001")


async def test_foreign_account_indistinguishable_from_nonexistent(bank_service):
    with pytest.raises(NotOwnedError) as foreign:
        await bank_service.get_balance(AISHA, "ACC-2001")
    with pytest.raises(NotOwnedError) as missing:
        await bank_service.get_balance(AISHA, "ACC-9999")
    assert foreign.value.message == missing.value.message


async def test_foreign_account_rejected_before_any_data(bank_service):
    with pytest.raises(NotOwnedError):
        await bank_service.get_transactions(AISHA, "ACC-2001", limit=1)
    with pytest.raises(NotOwnedError):
        await bank_service.spending_summary(AISHA, account_id="ACC-3001")


async def test_aggregate_scope_is_owned_accounts_only(bank_service, truth):
    structured, _ = await bank_service.spending_summary(
        AISHA, from_date="2026-10-01", to_date="2026-10-31"
    )
    ids = {account["account_id"] for account in structured["scope"]["accounts"]}
    assert ids == {"ACC-1001", "ACC-1002"}
    payload = str(structured)
    assert "ACC-2001" not in payload and "C002" not in payload


# --- account payload hygiene (SE-6) ---


async def test_accounts_mask_numbers_and_hide_customer_id(bank_service):
    structured, text = await bank_service.list_accounts(AISHA)
    assert structured["count"] == 2
    for account in structured["accounts"]:
        assert set(account) == {
            "account_id",
            "type",
            "account_number_masked",
            "currency",
            "balance",
        }
        assert account["account_number_masked"].startswith("…")
    assert "C001" not in str(structured)
    assert not re.search(r"AE\d{6,}", text)  # no full account numbers anywhere


async def test_balance_matches_truth(bank_service, truth):
    structured, _ = await bank_service.get_balance(AISHA, "ACC-1001")
    assert structured["balance"] == truth["C001.ACC-1001.balance"]


# --- input validation (MC-4) ---


@pytest.mark.parametrize("limit", [0, 101, -5])
async def test_limit_bounds(bank_service, limit):
    with pytest.raises(InvalidInputError):
        await bank_service.get_transactions(AISHA, "ACC-1001", limit=limit)


async def test_invalid_dates_and_inverted_range(bank_service):
    with pytest.raises(InvalidInputError):
        await bank_service.get_transactions(AISHA, "ACC-1001", from_date="31-10-2026")
    with pytest.raises(InvalidInputError):
        await bank_service.get_transactions(
            AISHA, "ACC-1001", from_date="2026-10-31", to_date="2026-10-01"
        )


async def test_invalid_category_and_status(bank_service):
    with pytest.raises(InvalidInputError):
        await bank_service.get_transactions(AISHA, "ACC-1001", category="crypto")
    with pytest.raises(InvalidInputError):
        await bank_service.get_transactions(AISHA, "ACC-1001", status="disputed")
    with pytest.raises(InvalidInputError):
        await bank_service.spending_summary(AISHA, category="crypto")


# --- pagination (MC-5) ---


async def test_transparent_pagination_past_upstream_50(bank_service, truth):
    total = truth["C001.ACC-1002.transactions.count"]
    assert total > 100  # the seed guarantees multi-page histories

    structured, _ = await bank_service.get_transactions(AISHA, "ACC-1002", limit=100)
    assert structured["count"] == 100
    assert structured["next_cursor"] is not None

    rest, _ = await bank_service.get_transactions(
        AISHA, "ACC-1002", limit=100, cursor=structured["next_cursor"]
    )
    assert structured["count"] + rest["count"] == min(total, 200)
    first_ids = {row["transaction_id"] for row in structured["items"]}
    assert first_ids.isdisjoint({row["transaction_id"] for row in rest["items"]})


# --- filters, Arabic data, injection data (UI-6, SE-3) ---


async def test_dining_october_filter_and_arabic_merchants(bank_service, truth):
    structured, _ = await bank_service.get_transactions(
        AISHA, "ACC-1002", from_date="2026-10-01", to_date="2026-10-31", category="dining"
    )
    expected = truth["C001.ACC-1002.transactions.count.dining.2026-10"]
    assert structured["count"] == expected
    merchants = {row["merchant_name"] for row in structured["items"]}
    assert "مطعم الواحة" in merchants  # preserved verbatim, not transliterated


async def test_injected_description_stays_in_data_not_in_summary(bank_service):
    structured, text = await bank_service.get_transactions(
        AISHA, "ACC-1002", from_date="2026-10-27", to_date="2026-10-27"
    )
    descriptions = " ".join(row["description"] for row in structured["items"])
    assert "Ignore all previous instructions" in descriptions  # data is preserved…
    assert "Ignore all previous instructions" not in text  # …but never echoed in summaries


async def test_empty_state_for_dormant_account(bank_service, truth):
    structured, text = await bank_service.get_transactions(
        LINA, "ACC-3001", from_date="2026-10-01", to_date="2026-10-31"
    )
    assert structured["count"] == truth["C003.ACC-3001.transactions.count.2026-10"] == 0
    assert "No transactions" in text


# --- Decimal business rule (MC-9): refunds, pending/reversed exclusion ---


@pytest.mark.parametrize(
    ("category", "month", "truth_key"),
    [
        # refund in the same period reduces the category
        ("dining", "2026-09", "C001.ACC-1002.spend.dining.2026-09"),
        # refund booked later than its August purchase counts in September
        ("groceries", "2026-09", "C001.ACC-1002.spend.groceries.2026-09"),
        # pending refund and pending purchase do not count in October
        ("groceries", "2026-10", "C001.ACC-1002.spend.groceries.2026-10"),
        ("fuel", "2026-10", "C001.ACC-1002.spend.fuel.2026-10"),
    ],
)
async def test_category_spend_matches_truth(bank_service, truth, category, month, truth_key):
    import calendar

    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    structured, _ = await bank_service.spending_summary(
        AISHA,
        account_id="ACC-1002",
        from_date=f"{month}-01",
        to_date=f"{month}-{last_day:02d}",
        category=category,
    )
    assert structured["amount"] == truth[truth_key]
    assert structured["by_category"][category] == truth[truth_key]


async def test_aggregate_october_matches_truth(bank_service, truth):
    structured, _ = await bank_service.spending_summary(
        AISHA, from_date="2026-10-01", to_date="2026-10-31"
    )
    for category in ("groceries", "dining", "fuel"):
        assert structured["by_category"][category] == truth[f"C001.ALL.spend.{category}.2026-10"]
    assert structured["total"] == truth["C001.ALL.spend_total.2026-10"]
    assert structured["count"] == truth["C001.ALL.transactions.count.2026-10"]


async def test_largest_expense_excludes_reversed(bank_service, truth):
    structured, _ = await bank_service.spending_summary(
        OMAR, from_date="2026-08-01", to_date="2026-08-31"
    )
    largest = structured["largest_expense"]
    assert largest["amount"] == truth["C002.ACC-2001.largest_expense.2026-08.amount"]
    assert largest["merchant_name"] == "Falcon Airways"  # not the reversed 12,000 resort row


# --- resilience (MC-6) ---


async def test_retries_transient_failures_then_succeeds(mock_app):
    state = {"failures": 2}
    asgi = httpx.ASGITransport(app=mock_app)

    class FlakyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if state["failures"] > 0 and request.url.path != "/sessions/introspect":
                state["failures"] -= 1
                return httpx.Response(503, json={"detail": "Injected failure (chaos)"})
            return await asgi.handle_async_request(request)

    service = BankService(make_bank_client(FlakyTransport()))
    structured, _ = await service.get_balance(AISHA, "ACC-1001")
    assert structured["account_id"] == "ACC-1001"
    assert state["failures"] == 0


async def test_persistent_failure_is_a_safe_error(mock_app):
    class DownTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(503, json={"detail": "internal secret"})

    service = BankService(make_bank_client(DownTransport()))
    with pytest.raises(UpstreamUnavailableError) as excinfo:
        await service.list_accounts(AISHA)
    assert "internal secret" not in excinfo.value.message
