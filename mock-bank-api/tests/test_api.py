import time

import pytest

ADMIN = {"X-Admin-Key": "test-admin-key"}


def transactions(client, account_id="ACC-1002", **params):
    response = client.get(f"/accounts/{account_id}/transactions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_health_needs_no_key(client):
    response = client.get("/health", headers={"X-Service-Key": ""})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/sessions/introspect"),
        ("get", "/customers/C001"),
        ("get", "/customers/C001/accounts"),
        ("get", "/accounts/ACC-1001"),
        ("get", "/accounts/ACC-1001/transactions"),
        ("get", "/categories"),
    ],
)
@pytest.mark.parametrize("headers", [{"X-Service-Key": "wrong"}, {"X-Service-Key": ""}])
def test_bank_endpoints_reject_a_bad_service_key(client, method, path, headers):
    assert client.request(method, path, headers=headers).status_code == 401


def test_introspect_active_token(client):
    body = client.post("/sessions/introspect", json={"token": "tok_aisha_valid"}).json()
    assert body["active"] is True
    assert body["customer_id"] == "C001"
    assert body["expires_at"].startswith("2027-12-31")
    assert body["scopes"]


def test_introspect_expired_token_returns_no_identity(client):
    body = client.post("/sessions/introspect", json={"token": "tok_aisha_expired"}).json()
    assert body["active"] is False
    assert body["customer_id"] is None
    assert body["expires_at"].startswith("2026-10-01")


def test_introspect_unknown_token(client):
    body = client.post("/sessions/introspect", json={"token": "nope"}).json()
    assert body == {"active": False, "customer_id": None, "expires_at": None, "scopes": []}


def test_get_customer(client):
    assert client.get("/customers/C002").json() == {
        "customer_id": "C002",
        "name": "Omar Haddad",
        "preferred_language": "ar",
    }
    assert client.get("/customers/C999").status_code == 404


def test_list_customer_accounts(client):
    items = client.get("/customers/C001/accounts").json()["items"]
    assert [(a["account_id"], a["type"]) for a in items] == [
        ("ACC-1001", "savings"),
        ("ACC-1002", "current"),
    ]
    assert client.get("/customers/C999/accounts").status_code == 404


def test_get_account(client):
    account = client.get("/accounts/ACC-2001").json()
    assert account["customer_id"] == "C002"
    assert account["currency"] == "AED"
    assert float(account["balance"]) > 0
    assert client.get("/accounts/ACC-9999").status_code == 404


def test_api_is_a_trusted_back_office_api(client):
    # Isolation belongs to the candidate's MCP server, so any customer's data is readable here.
    assert client.get("/accounts/ACC-2001/transactions").status_code == 200
    assert client.get("/accounts/ACC-1001/transactions").status_code == 200


def test_transaction_model_and_order(client):
    items = transactions(client, limit=50)["items"]
    assert set(items[0]) == {
        "transaction_id",
        "account_id",
        "booked_at",
        "amount",
        "currency",
        "status",
        "merchant_name",
        "category",
        "description",
        "reference",
    }
    booked = [item["booked_at"] for item in items]
    assert booked == sorted(booked, reverse=True)
    assert {item["currency"] for item in items} == {"AED"}
    assert items[0]["booked_at"].endswith("+04:00")


def test_default_limit_is_20(client):
    assert len(transactions(client)["items"]) == 20


@pytest.mark.parametrize("limit", [0, 51, -1])
def test_limit_out_of_range(client, limit):
    response = client.get("/accounts/ACC-1002/transactions", params={"limit": limit})
    assert response.status_code == 422


def test_pagination_walks_every_row_once(client):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        page = transactions(client, **params)
        seen += [item["transaction_id"] for item in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages >= 3
    assert len(seen) == len(set(seen))
    assert len(seen) > 100


@pytest.mark.parametrize("cursor", ["%%%", "bm90LWEtbnVtYmVy", "LTU="])
def test_invalid_cursor(client, cursor):
    response = client.get("/accounts/ACC-1002/transactions", params={"cursor": cursor})
    assert response.status_code == 400


def test_filters(client):
    october = transactions(client, **{"from": "2026-10-01", "to": "2026-10-31", "limit": 50})
    assert october["items"]
    assert all(item["booked_at"].startswith("2026-10") for item in october["items"])

    dining = transactions(client, category="dining", limit=50)["items"]
    assert dining and {item["category"] for item in dining} == {"dining"}

    pending = transactions(client, status="pending", limit=50)["items"]
    assert pending and {item["status"] for item in pending} == {"pending"}


def test_date_filter_is_inclusive_on_both_days(client):
    day = transactions(client, limit=1)["items"][0]["booked_at"][:10]
    items = transactions(client, **{"from": day, "to": day})["items"]
    assert items and all(item["booked_at"].startswith(day) for item in items)


def test_invalid_filters(client):
    path = "/accounts/ACC-1002/transactions"
    assert client.get(path, params={"from": "not-a-date"}).status_code == 422
    assert client.get(path, params={"category": "crypto"}).status_code == 422
    assert client.get(path, params={"status": "done"}).status_code == 422
    assert client.get(path, params={"from": "2026-10-02", "to": "2026-10-01"}).status_code == 400
    assert client.get("/accounts/ACC-9999/transactions").status_code == 404


def test_categories(client):
    items = client.get("/categories").json()["items"]
    assert len(items) == 13
    assert {"groceries", "refund", "uncategorised"} <= set(items)


def test_chaos_needs_the_admin_key(client):
    assert client.post("/_admin/chaos", json={"error_rate": 1.0}).status_code == 401
    wrong = {"X-Admin-Key": "wrong"}
    assert client.post("/_admin/chaos", json={"error_rate": 1.0}, headers=wrong).status_code == 401


@pytest.mark.parametrize(
    "body", [{"error_rate": 1.5}, {"error_rate": -0.1}, {"latency_ms": -1}, {"latency_ms": "x"}]
)
def test_chaos_validates_its_body(client, body):
    assert client.post("/_admin/chaos", json=body, headers=ADMIN).status_code == 422


def test_chaos_injects_errors_but_spares_health_and_admin(client):
    response = client.post("/_admin/chaos", json={"error_rate": 1.0}, headers=ADMIN)
    assert response.json() == {"error_rate": 1.0, "latency_ms": 0}
    assert client.get("/categories").status_code == 503
    assert client.get("/health").status_code == 200
    client.post("/_admin/chaos", json={"error_rate": 0.0}, headers=ADMIN)
    assert client.get("/categories").status_code == 200


def test_chaos_injects_latency(client):
    client.post("/_admin/chaos", json={"latency_ms": 200}, headers=ADMIN)
    start = time.perf_counter()
    assert client.get("/categories").status_code == 200
    assert time.perf_counter() - start >= 0.19


def test_openapi_document(client):
    response = client.get("/openapi.json", headers={"X-Service-Key": ""})
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert {
        "/health",
        "/sessions/introspect",
        "/customers/{customer_id}",
        "/customers/{customer_id}/accounts",
        "/accounts/{account_id}",
        "/accounts/{account_id}/transactions",
        "/categories",
        "/_admin/chaos",
    } <= set(paths)
