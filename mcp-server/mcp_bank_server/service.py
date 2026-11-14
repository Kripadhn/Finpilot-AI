"""Business/service layer of the MCP server.

Identity rules (MC-3, SE-1, SE-2):
- the session token is introspected on EVERY operation; inactive/unknown/under-scoped
  sessions raise SessionInvalidError and return no data;
- customer_id comes only from the introspection response;
- ownership is verified before every account-scoped request; a non-owned account raises the
  same error as a nonexistent one (no existence oracle).

All money maths uses Decimal (MC-9). The spend rule mirrors scripts/compute_truth.py: posted
debits minus posted refunds linked (via `reference`) to the original purchase's category;
pending and reversed rows are excluded; a refund counts in its own booking period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from bank_shared.bank_client import BankClient
from bank_shared.errors import InvalidInputError, NotOwnedError, SessionInvalidError
from bank_shared.logging import mask_account_number
from bank_shared.money import amount as dec
from bank_shared.money import fmt_aed, to_number

CATEGORIES = [
    "groceries",
    "fuel",
    "dining",
    "utilities",
    "rent",
    "salary",
    "transfer",
    "entertainment",
    "travel",
    "shopping",
    "fees",
    "refund",
    "uncategorised",
]
STATUSES = ["posted", "pending", "reversed"]
LIMIT_MIN, LIMIT_MAX = 1, 100

SCOPE_ACCOUNTS = "accounts:read"
SCOPE_TRANSACTIONS = "transactions:read"


@dataclass
class Identity:
    customer_id: str
    scopes: list[str]
    accounts: list[dict[str, Any]]

    def owned(self, account_id: str) -> dict[str, Any]:
        for account in self.accounts:
            if account["account_id"] == account_id:
                return account
        # Same message whether the account belongs to someone else or does not exist.
        raise NotOwnedError()


def safe_account(account: dict[str, Any]) -> dict[str, Any]:
    """Account payload safe for tool results: masked number, no customer_id."""
    return {
        "account_id": account["account_id"],
        "type": account["type"],
        "account_number_masked": mask_account_number(account["account_number"]),
        "currency": account["currency"],
        "balance": to_number(dec(account["balance"])),
    }


def _parse_iso(value: str | None, name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise InvalidInputError(f"{name} must be an ISO date (YYYY-MM-DD).") from error


def validate_period(from_date: str | None, to_date: str | None) -> tuple[date | None, date | None]:
    start, end = _parse_iso(from_date, "from_date"), _parse_iso(to_date, "to_date")
    if start and end and start > end:
        raise InvalidInputError("from_date must not be after to_date.")
    return start, end


def validate_category(category: str | None) -> str | None:
    if category is None or category == "":
        return None
    if category not in CATEGORIES:
        raise InvalidInputError(f"Unknown category. Valid categories: {', '.join(CATEGORIES)}.")
    return category


def validate_status(status: str | None) -> str | None:
    if status is None or status == "":
        return None
    if status not in STATUSES:
        raise InvalidInputError(f"Unknown status. Valid statuses: {', '.join(STATUSES)}.")
    return status


def validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not LIMIT_MIN <= limit <= LIMIT_MAX:
        raise InvalidInputError(f"limit must be an integer between {LIMIT_MIN} and {LIMIT_MAX}.")
    return limit


def spend(rows: list[dict], by_id: dict[str, dict], category: str | None = None) -> Decimal:
    """Spend rule, identical to scripts/compute_truth.py. None means every category."""
    total = Decimal(0)
    for row in rows:
        if row["status"] != "posted":
            continue
        value = dec(row["amount"])
        if value < 0 and category in (None, row["category"]):
            total -= value
        elif row["category"] == "refund" and value > 0:
            original = by_id.get(row["reference"])
            if original and category in (None, original["category"]):
                total -= value
    return total


def largest_expense(rows: list[dict]) -> dict[str, Any] | None:
    debits = [row for row in rows if row["status"] == "posted" and dec(row["amount"]) < 0]
    if not debits:
        return None
    top = min(debits, key=lambda r: (dec(r["amount"]), r["transaction_id"]))
    return {
        "transaction_id": top["transaction_id"],
        "merchant_name": top["merchant_name"],
        "amount": to_number(-dec(top["amount"])),
        "booked_at": top["booked_at"],
        "category": top["category"],
    }


class BankService:
    def __init__(self, client: BankClient) -> None:
        self._client = client

    async def authenticate(self, token: str | None, required_scope: str) -> Identity:
        """Introspect on every call (SE-1). One safe error for every failure mode."""
        if not token:
            raise SessionInvalidError()
        info = await self._client.introspect(token)
        if not info.get("active") or not info.get("customer_id"):
            raise SessionInvalidError()
        scopes = info.get("scopes") or []
        if required_scope not in scopes:
            raise SessionInvalidError()
        accounts = await self._client.list_accounts(info["customer_id"])
        return Identity(customer_id=info["customer_id"], scopes=scopes, accounts=accounts)

    async def list_accounts(self, token: str | None) -> tuple[dict[str, Any], str]:
        identity = await self.authenticate(token, SCOPE_ACCOUNTS)
        items = [safe_account(account) for account in identity.accounts]
        labels = ", ".join(
            f"{item['type']} {item['account_number_masked']}"
            f" ({fmt_aed(Decimal(str(item['balance'])))})"
            for item in items
        )
        return {"accounts": items, "count": len(items)}, f"{len(items)} account(s): {labels}."

    async def get_balance(self, token: str | None, account_id: str) -> tuple[dict[str, Any], str]:
        identity = await self.authenticate(token, SCOPE_ACCOUNTS)
        identity.owned(account_id)
        fresh = await self._client.get_account(account_id)
        payload = safe_account(fresh)
        text = (
            f"Balance of {payload['type']} account {payload['account_number_masked']}: "
            f"{fmt_aed(dec(fresh['balance']))}."
        )
        return payload, text

    async def get_transactions(
        self,
        token: str | None,
        account_id: str,
        from_date: str | None = None,
        to_date: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        identity = await self.authenticate(token, SCOPE_TRANSACTIONS)
        account = identity.owned(account_id)
        start, end = validate_period(from_date, to_date)
        category = validate_category(category)
        status = validate_status(status)
        limit = validate_limit(limit)

        items, next_cursor = await self._client.transactions(
            account_id,
            from_date=start,
            to_date=end,
            category=category,
            status=status,
            max_items=limit,
            cursor=cursor,
        )
        rows = [
            {
                "transaction_id": row["transaction_id"],
                "booked_at": row["booked_at"],
                "amount": to_number(dec(row["amount"])),
                "currency": row["currency"],
                "status": row["status"],
                "merchant_name": row["merchant_name"],
                "category": row["category"],
                "description": row["description"],
                "reference": row["reference"],
            }
            for row in items
        ]
        structured = {
            "account": safe_account(account),
            "period": {"from": from_date or None, "to": to_date or None},
            "category": category,
            "status": status,
            "count": len(rows),
            "items": rows,
            "next_cursor": next_cursor,
        }
        scope_bits = [f"{account['type']} account {mask_account_number(account['account_number'])}"]
        if category:
            scope_bits.append(f"category {category}")
        if start or end:
            scope_bits.append(f"period {from_date or '…'} to {to_date or '…'}")
        text = f"{len(rows)} transaction(s) on {', '.join(scope_bits)} (newest first)."
        if not rows:
            text = f"No transactions found on {', '.join(scope_bits)}."
        return structured, text

    async def spending_summary(
        self,
        token: str | None,
        account_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        category: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        identity = await self.authenticate(token, SCOPE_TRANSACTIONS)
        start, end = validate_period(from_date, to_date)
        category = validate_category(category)
        targets = [identity.owned(account_id)] if account_id else identity.accounts

        # Full history per account: refunds may reference purchases outside the period, and
        # periods can span more than one upstream page. Seeded histories are small (< 300 rows).
        all_rows: list[dict[str, Any]] = []
        for account in targets:
            rows, _ = await self._client.transactions(account["account_id"])
            all_rows.extend(rows)
        by_id = {row["transaction_id"]: row for row in all_rows}

        def in_period(row: dict[str, Any]) -> bool:
            day = row["booked_at"][:10]
            return (start is None or day >= start.isoformat()) and (
                end is None or day <= end.isoformat()
            )

        period_rows = [row for row in all_rows if in_period(row)]
        wanted = [category] if category else CATEGORIES
        by_category = {
            cat: to_number(spend(period_rows, by_id, cat))
            for cat in wanted
            if cat not in ("refund",)
        }
        total = to_number(spend(period_rows, by_id, category))
        structured = {
            "scope": {"accounts": [safe_account(account) for account in targets]},
            "period": {"from": from_date or None, "to": to_date or None},
            "category": category,
            "currency": "AED",
            "by_category": by_category,
            "total": total,
            "amount": total,
            "count": len(period_rows),
            "largest_expense": largest_expense(period_rows),
        }
        period_text = (
            f"{from_date or 'start'} to {to_date or 'today'}" if (start or end) else "all history"
        )
        scope_text = (
            f"{targets[0]['type']} account {mask_account_number(targets[0]['account_number'])}"
            if account_id
            else f"{len(targets)} account(s)"
        )
        if category:
            text = (
                f"Spend on {category} ({period_text}, {scope_text}): "
                f"{fmt_aed(Decimal(str(total)))}."
            )
        else:
            top = sorted(by_category.items(), key=lambda kv: -kv[1])[:3]
            top_text = ", ".join(f"{cat} {fmt_aed(Decimal(str(val)))}" for cat, val in top if val)
            text = f"Total spend ({period_text}, {scope_text}): {fmt_aed(Decimal(str(total)))}." + (
                f" Top categories: {top_text}." if top_text else ""
            )
        return structured, text

    async def categories(self) -> list[str]:
        return await self._client.get_categories()
