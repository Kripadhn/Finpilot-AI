"""Mock core banking API: a trusted back-office API.

A valid service key can read any customer. This is intentional: customer isolation is the job
of the MCP server that the candidate builds.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import random
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

SERVICE_KEY = os.environ["MOCK_SERVICE_KEY"]
ADMIN_KEY = os.environ["MOCK_ADMIN_KEY"]
FIXED_TODAY = date.fromisoformat(os.getenv("FIXED_TODAY", "2026-10-31"))
DB_PATH = Path(os.getenv("BANK_DB_PATH", Path(__file__).resolve().parents[2] / "data" / "bank.db"))

CHAOS_EXEMPT_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/_admin/chaos"}


class TransactionStatus(StrEnum):
    posted = "posted"
    pending = "pending"
    reversed = "reversed"


class Category(StrEnum):
    groceries = "groceries"
    fuel = "fuel"
    dining = "dining"
    utilities = "utilities"
    rent = "rent"
    salary = "salary"
    transfer = "transfer"
    entertainment = "entertainment"
    travel = "travel"
    shopping = "shopping"
    fees = "fees"
    refund = "refund"
    uncategorised = "uncategorised"


class IntrospectRequest(BaseModel):
    token: str


class SessionInfo(BaseModel):
    active: bool
    customer_id: str | None = None
    expires_at: str | None = None
    scopes: list[str] = []


class Customer(BaseModel):
    customer_id: str
    name: str
    preferred_language: str


class Account(BaseModel):
    account_id: str
    customer_id: str
    type: str
    account_number: str
    currency: str
    balance: str = Field(description="Decimal string. Posted transactions only.")


class AccountList(BaseModel):
    items: list[Account]


class Transaction(BaseModel):
    transaction_id: str
    account_id: str
    booked_at: str = Field(description="ISO 8601 datetime, Asia/Dubai")
    amount: str = Field(description="Decimal string. Negative = debit, positive = credit")
    currency: str
    status: TransactionStatus
    merchant_name: str
    category: Category
    description: str = Field(description="Untrusted free text")
    reference: str = Field(
        description="For category `refund`: the transaction_id of the original purchase"
    )


class TransactionPage(BaseModel):
    items: list[Transaction]
    next_cursor: str | None


class CategoryList(BaseModel):
    items: list[Category]


class ChaosConfig(BaseModel):
    error_rate: float = Field(0.0, ge=0.0, le=1.0)
    latency_ms: int = Field(0, ge=0, le=60_000)


chaos = ChaosConfig()
service_key_header = APIKeyHeader(name="X-Service-Key", auto_error=False)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_service_key(key: str | None = Security(service_key_header)) -> None:
    if key is None or not secrets.compare_digest(key.encode(), SERVICE_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Service-Key")


def require_admin_key(key: str | None = Security(admin_key_header)) -> None:
    if key is None or not secrets.compare_digest(key.encode(), ADMIN_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    # A read-only connection: nothing in this API can change the seeded data.
    conn = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def account_or_404(account_id: str) -> dict:
    rows = query("SELECT * FROM accounts WHERE account_id = ?", (account_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Account not found")
    return rows[0]


def customer_or_404(customer_id: str) -> dict:
    rows = query("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Customer not found")
    return rows[0]


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (binascii.Error, UnicodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid cursor") from error
    if not 0 <= offset < 2**31:
        raise HTTPException(status_code=400, detail="Invalid cursor")
    return offset


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if not DB_PATH.exists():
        raise RuntimeError(f"{DB_PATH} does not exist. Run `make seed` first.")
    yield


app = FastAPI(
    title="DVT Mock Core Banking API",
    version="1.0.0",
    description="Trusted back-office API with synthetic data. Customer isolation is not here.",
    lifespan=lifespan,
)


@app.middleware("http")
async def inject_chaos(request: Request, call_next) -> Response:
    if request.url.path not in CHAOS_EXEMPT_PATHS:
        if chaos.latency_ms:
            await asyncio.sleep(chaos.latency_ms / 1000)
        if random.random() < chaos.error_rate:
            return JSONResponse(status_code=503, content={"detail": "Injected failure (chaos)"})
    return await call_next(request)


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_admin/chaos", tags=["operations"], dependencies=[Depends(require_admin_key)])
def set_chaos(config: ChaosConfig) -> ChaosConfig:
    chaos.error_rate = config.error_rate
    chaos.latency_ms = config.latency_ms
    return chaos


bank = APIRouter(dependencies=[Depends(require_service_key)])


@bank.post("/sessions/introspect", tags=["sessions"])
def introspect(body: IntrospectRequest) -> SessionInfo:
    rows = query("SELECT * FROM sessions WHERE token = ?", (body.token,))
    if not rows:
        return SessionInfo(active=False)
    session = rows[0]
    expires_at = session["expires_at"]
    if date.fromisoformat(expires_at[:10]) < FIXED_TODAY:
        # expires_at lets the caller say "expired" rather than "unknown". No identity is returned.
        return SessionInfo(active=False, expires_at=expires_at)
    return SessionInfo(
        active=True,
        customer_id=session["customer_id"],
        expires_at=expires_at,
        scopes=json.loads(session["scopes"]),
    )


@bank.get("/customers/{customer_id}", tags=["customers"])
def get_customer(customer_id: str) -> Customer:
    return Customer(**customer_or_404(customer_id))


@bank.get("/customers/{customer_id}/accounts", tags=["customers"])
def list_customer_accounts(customer_id: str) -> AccountList:
    customer_or_404(customer_id)
    rows = query("SELECT * FROM accounts WHERE customer_id = ? ORDER BY account_id", (customer_id,))
    return AccountList(items=[Account(**row) for row in rows])


@bank.get("/accounts/{account_id}", tags=["accounts"])
def get_account(account_id: str) -> Account:
    return Account(**account_or_404(account_id))


@bank.get("/accounts/{account_id}/transactions", tags=["accounts"])
def list_transactions(
    account_id: str,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    category: Category | None = None,
    status: TransactionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: str | None = None,
) -> TransactionPage:
    account_or_404(account_id)
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=400, detail="`from` must not be after `to`")
    offset = decode_cursor(cursor)

    where = ["account_id = ?"]
    params: list[str | int] = [account_id]
    if from_date:
        where.append("substr(booked_at, 1, 10) >= ?")
        params.append(from_date.isoformat())
    if to_date:
        where.append("substr(booked_at, 1, 10) <= ?")
        params.append(to_date.isoformat())
    if category:
        where.append("category = ?")
        params.append(category.value)
    if status:
        where.append("status = ?")
        params.append(status.value)

    # One extra row tells us whether another page exists.
    rows = query(
        f"SELECT * FROM transactions WHERE {' AND '.join(where)} "
        "ORDER BY booked_at DESC, transaction_id DESC LIMIT ? OFFSET ?",
        [*params, limit + 1, offset],
    )
    next_cursor = encode_cursor(offset + limit) if len(rows) > limit else None
    return TransactionPage(
        items=[Transaction(**row) for row in rows[:limit]], next_cursor=next_cursor
    )


@bank.get("/categories", tags=["reference"])
def list_categories() -> CategoryList:
    return CategoryList(items=list(Category))


app.include_router(bank)
