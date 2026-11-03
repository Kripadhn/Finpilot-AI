"""Write data/ground_truth.json from the seeded database. Flat keys, for example:

C001.ACC-1002.spend.groceries.2026-09   spend under the rule in README section 6.3
C001.ALL.spend.groceries.2026-09        the same across all accounts of the customer
C001.ACC-1002.spend_total.2026-09       all categories together
C001.ACC-1002.spend.groceries.all       the whole seeded history
C001.ACC-1002.balance
C001.ACC-1002.transactions.count        also .count.2026-09 and .count.groceries.2026-09
C001.ACC-1002.largest_expense.2026-09.amount    also .transaction_id and .merchant_name
C001.ACC-1002.last_20.count             the 20 newest rows; also .last_20.spend.fuel
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MONTHS = [f"2026-{month:02d}" for month in range(5, 11)]
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


def to_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def spend(rows: list[dict], by_id: dict[str, dict], category: str | None = None) -> Decimal:
    """Posted debits minus posted refunds linked to the category. None means every category.

    A refund counts in the rows that contain the refund, even when its purchase is elsewhere.
    """
    total = Decimal(0)
    for row in rows:
        if row["status"] != "posted":
            continue
        amount = Decimal(row["amount"])
        if amount < 0 and category in (None, row["category"]):
            total -= amount
        elif row["category"] == "refund" and amount > 0:
            original = by_id.get(row["reference"])
            if original and category in (None, original["category"]):
                total -= amount
    return total


def month_of(row: dict) -> str:
    return row["booked_at"][:7]


def newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["booked_at"], r["transaction_id"]), reverse=True)


def truth_for_rows(prefix: str, rows: list[dict], by_id: dict[str, dict]) -> dict:
    truth: dict = {f"{prefix}.transactions.count": len(rows)}
    for category in CATEGORIES:
        truth[f"{prefix}.spend.{category}.all"] = to_number(spend(rows, by_id, category))

    for month in MONTHS:
        in_month = [row for row in rows if month_of(row) == month]
        truth[f"{prefix}.transactions.count.{month}"] = len(in_month)
        truth[f"{prefix}.spend_total.{month}"] = to_number(spend(in_month, by_id))
        for category in CATEGORIES:
            truth[f"{prefix}.spend.{category}.{month}"] = to_number(
                spend(in_month, by_id, category)
            )
            truth[f"{prefix}.transactions.count.{category}.{month}"] = sum(
                row["category"] == category for row in in_month
            )

        debits = [
            row for row in in_month if row["status"] == "posted" and Decimal(row["amount"]) < 0
        ]
        if debits:
            largest = min(debits, key=lambda r: (Decimal(r["amount"]), r["transaction_id"]))
            truth[f"{prefix}.largest_expense.{month}.amount"] = to_number(
                -Decimal(largest["amount"])
            )
            truth[f"{prefix}.largest_expense.{month}.transaction_id"] = largest["transaction_id"]
            truth[f"{prefix}.largest_expense.{month}.merchant_name"] = largest["merchant_name"]
    return truth


def build_truth(accounts: list[dict], transactions: list[dict]) -> dict:
    by_id = {row["transaction_id"]: row for row in transactions}
    truth: dict = {}
    customers = sorted({account["customer_id"] for account in accounts})
    for customer in customers:
        owned = [account for account in accounts if account["customer_id"] == customer]
        for account in owned:
            prefix = f"{customer}.{account['account_id']}"
            rows = [row for row in transactions if row["account_id"] == account["account_id"]]
            truth.update(truth_for_rows(prefix, rows, by_id))
            truth[f"{prefix}.balance"] = to_number(Decimal(account["balance"]))

            last_20 = newest_first(rows)[:20]
            truth[f"{prefix}.last_20.count"] = len(last_20)
            for category in CATEGORIES:
                truth[f"{prefix}.last_20.spend.{category}"] = to_number(
                    spend(last_20, by_id, category)
                )

        owned_ids = {account["account_id"] for account in owned}
        all_rows = [row for row in transactions if row["account_id"] in owned_ids]
        truth.update(truth_for_rows(f"{customer}.ALL", all_rows, by_id))
        truth[f"{customer}.ALL.balance"] = to_number(
            sum((Decimal(account["balance"]) for account in owned), Decimal(0))
        )
    return truth


def load_rows(db_path: Path) -> tuple[list[dict], list[dict]]:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        accounts = [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY account_id")]
        transactions = [
            dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY transaction_id")
        ]
    finally:
        conn.close()
    return accounts, transactions


def main() -> None:
    parser = argparse.ArgumentParser(description="Write data/ground_truth.json")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "bank.db")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "ground_truth.json")
    args = parser.parse_args()

    truth = build_truth(*load_rows(args.db))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(truth, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    print(f"Wrote {len(truth)} keys to {args.out}")


if __name__ == "__main__":
    main()
