"""Deterministic language helpers: intent cues, account resolution, argument sanitising.

The model proposes tools; this module keeps it honest. Dates, account references, limits and
categories mentioned by the customer are resolved in code against the fixed clock and the
customer's own accounts, and the model's arguments are corrected to match (AG-5, AG-6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from bank_shared.dates import resolve_period

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
# words customers actually use -> category
CATEGORY_SYNONYMS = {
    "grocery": "groceries",
    "food shopping": "groceries",
    "petrol": "fuel",
    "gas": "fuel",
    "restaurant": "dining",
    "restaurants": "dining",
    "eating out": "dining",
    "utility": "utilities",
    "bills": "utilities",
    "entertainment": "entertainment",
    "movies": "entertainment",
    "clothes": "shopping",
    "fee": "fees",
    "charges": "fees",
    "uncategorized": "uncategorised",
}

_LAST_N_RE = re.compile(r"\blast\s+(\d{1,3})\b")
_ACC_ID_RE = re.compile(r"\bACC-\d+\b")
_FOLLOW_UP_RE = re.compile(
    r"\b(?:of (?:that|those|these|them|it)|was (?:that|those)|from (?:that|those) list)\b"
)


def mentioned_category(message: str) -> str | None:
    lowered = message.lower()
    for category in CATEGORIES:
        if re.search(rf"\b{category}\b", lowered):
            return category
    for word, category in CATEGORY_SYNONYMS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return category
    return None


def mentioned_limit(message: str) -> int | None:
    match = _LAST_N_RE.search(message.lower())
    if match:
        return max(1, min(100, int(match.group(1))))
    return None


def is_follow_up(message: str) -> bool:
    return bool(_FOLLOW_UP_RE.search(message.lower()))


@dataclass
class Intent:
    kind: str  # balance | transactions | spending | largest | other
    wants_single_account: bool


def classify(message: str) -> Intent:
    lowered = message.lower()
    if re.search(r"\b(biggest|largest|highest)\b.*\b(expense|purchase|transaction|spend)", lowered):
        return Intent("largest", wants_single_account=False)
    if re.search(r"\b(spend|spent|spending)\b", lowered):
        return Intent("spending", wants_single_account=False)
    if re.search(r"\bbalance", lowered):
        return Intent("balance", wants_single_account=True)
    if re.search(r"\b(transactions?|statements?|activity|payments)\b", lowered):
        return Intent("transactions", wants_single_account=True)
    return Intent("other", wants_single_account=False)


def resolve_account(message: str, accounts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The account the message names, or the only account; None when genuinely ambiguous."""
    lowered = message.lower()
    if match := _ACC_ID_RE.search(message):
        for account in accounts:
            if account["account_id"] == match.group(0):
                return account
        return None  # a foreign id never resolves (the guard refuses it earlier)
    by_type = [account for account in accounts if account["type"].lower() in lowered]
    if len(by_type) == 1:
        return by_type[0]
    if len(accounts) == 1:
        return accounts[0]
    return None


def wants_all_accounts(message: str) -> bool:
    lowered = message.lower()
    return bool(re.search(r"\b(both|all (?:my )?accounts|each account|every account)\b", lowered))


def explicit_singular_account(message: str) -> bool:
    """True for a bare "my account"/"the account": ambiguous when several accounts exist."""
    lowered = message.lower()
    if re.search(r"\b(savings|current)\b", lowered) or _ACC_ID_RE.search(message):
        return False
    return bool(re.search(r"\b(?:my|the)\s+account\b", lowered))


def period_args(message: str, today: date) -> dict[str, str]:
    period = resolve_period(message, today)
    if not period:
        return {}
    return {"from_date": period[0].isoformat(), "to_date": period[1].isoformat()}


def sanitize_tool_call(
    name: str,
    arguments: dict[str, Any],
    message: str,
    accounts: list[dict[str, Any]],
    today: date,
) -> dict[str, Any] | None:
    """Correct the model's arguments against what the customer actually said.

    Returns the safe arguments, or None when the call must not run (unknown tool or an
    account the message does not justify).
    """
    owned = {account["account_id"] for account in accounts}
    named = resolve_account(message, accounts)
    args: dict[str, Any] = {k: v for k, v in (arguments or {}).items() if v not in (None, "")}

    if name == "list_accounts":
        return {}
    if name not in {"get_balance", "get_transactions", "spending_summary"}:
        return None

    # account_id: only ever one of the customer's own, and only when the message supports it.
    account_id = args.get("account_id")
    if account_id is not None and account_id not in owned:
        account_id = None
    if named:
        account_id = named["account_id"]
    elif name == "spending_summary" and not wants_all_accounts(message):
        # No account named: aggregate (omit) rather than trust a model guess.
        account_id = account_id if len(accounts) == 1 else None
    if name in {"get_balance", "get_transactions"}:
        if account_id is None:
            return None  # single-account tools need a justified account
        args["account_id"] = account_id
    elif account_id:
        args["account_id"] = account_id
    else:
        args.pop("account_id", None)

    # dates: the fixed clock wins over whatever the model computed (AG-5).
    if name in {"get_transactions", "spending_summary"}:
        resolved = period_args(message, today)
        if resolved:
            args.update(resolved)
        for key in ("from_date", "to_date"):
            value = args.get(key)
            if value is not None:
                try:
                    date.fromisoformat(str(value))
                except ValueError:
                    args.pop(key, None)

        category = mentioned_category(message)
        if category:
            args["category"] = category
        elif args.get("category") not in CATEGORIES:
            args.pop("category", None)

    if name == "get_transactions":
        limit = mentioned_limit(message) or args.get("limit") or 20
        try:
            args["limit"] = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            args["limit"] = 20
        args.pop("status", None)  # the model has no reason to filter by status
        args.pop("cursor", None)

    allowed_keys = {
        "get_balance": {"account_id"},
        "get_transactions": {"account_id", "from_date", "to_date", "category", "limit"},
        "spending_summary": {"account_id", "from_date", "to_date", "category"},
    }[name]
    return {k: v for k, v in args.items() if k in allowed_keys}


def fallback_plan(
    message: str, accounts: list[dict[str, Any]], today: date
) -> list[tuple[str, dict[str, Any]]]:
    """Deterministic routing used when the model proposes nothing usable."""
    intent = classify(message)
    named = resolve_account(message, accounts)
    dates = period_args(message, today)
    category = mentioned_category(message)

    if intent.kind == "balance":
        targets = accounts if (wants_all_accounts(message) or not named) else [named]
        return [("get_balance", {"account_id": account["account_id"]}) for account in targets]
    if intent.kind in {"spending", "largest"}:
        args = dict(dates)
        if named:
            args["account_id"] = named["account_id"]
        if category and intent.kind == "spending":
            args["category"] = category
        return [("spending_summary", args)]
    if intent.kind == "transactions":
        targets = [named] if named else accounts
        limit = mentioned_limit(message)
        plans: list[tuple[str, dict[str, Any]]] = []
        for target in targets:
            args: dict[str, Any] = {"account_id": target["account_id"], **dates}
            if category:
                args["category"] = category
            # Aggregating or filtering: fetch enough rows that the merged view is complete.
            args["limit"] = limit if (limit and len(targets) == 1) else (limit or 100)
            plans.append(("get_transactions", args))
        return plans
    return []
