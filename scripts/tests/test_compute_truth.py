import json
from decimal import Decimal

from compute_truth import build_truth, load_rows, spend
from seed.generate import generate
from validate_cases import validate


def row(txn_id, amount, category, status="posted", reference="REF", month="09", day=10):
    return {
        "transaction_id": txn_id,
        "account_id": "ACC-1",
        "booked_at": f"2026-{month}-{day:02d}T10:00:00+04:00",
        "amount": amount,
        "status": status,
        "merchant_name": f"Merchant {txn_id}",
        "category": category,
        "reference": reference,
    }


ROWS = [
    row("T1", "-100.00", "dining"),
    row("T2", "-50.50", "dining", status="pending"),
    row("T3", "-70.00", "dining", status="reversed"),
    row("T4", "20.00", "refund", reference="T1", day=15),
    row("T5", "9.00", "refund", status="pending", reference="T1", day=16),
    # A purchase in August, refunded in September.
    row("T6", "-300.00", "dining", month="08"),
    row("T7", "30.00", "refund", reference="T6"),
    row("T8", "-40.00", "fuel"),
    row("T9", "15.00", "refund", reference="T8"),
    # A refund with no matching purchase counts in no category.
    row("T10", "5.00", "refund", reference="UNKNOWN"),
    row("T11", "1000.00", "salary"),
]
ACCOUNTS = [{"account_id": "ACC-1", "customer_id": "C9", "balance": "12.345"}]


def test_spend_rule():
    by_id = {r["transaction_id"]: r for r in ROWS}
    september = [r for r in ROWS if r["booked_at"].startswith("2026-09")]
    assert spend(september, by_id, "dining") == Decimal("50.00")  # 100 - 20 - 30
    assert spend(september, by_id, "fuel") == Decimal("25.00")
    assert spend(september, by_id, "salary") == 0
    assert spend(september, by_id) == Decimal("75.00")


def test_truth_keys_and_values():
    truth = build_truth(ACCOUNTS, ROWS)
    assert truth["C9.ACC-1.spend.dining.2026-09"] == 50.0
    assert truth["C9.ALL.spend.dining.2026-09"] == 50.0
    assert truth["C9.ACC-1.spend.dining.2026-08"] == 300.0
    assert truth["C9.ACC-1.spend.dining.all"] == 350.0
    assert truth["C9.ACC-1.spend_total.2026-09"] == 75.0
    assert truth["C9.ACC-1.spend.groceries.2026-09"] == 0.0
    assert truth["C9.ACC-1.balance"] == 12.35
    assert truth["C9.ACC-1.transactions.count"] == 11
    assert truth["C9.ACC-1.transactions.count.2026-08"] == 1
    assert truth["C9.ACC-1.transactions.count.dining.2026-09"] == 3
    assert truth["C9.ACC-1.last_20.count"] == 11
    assert truth["C9.ACC-1.last_20.spend.fuel"] == 25.0


def test_largest_expense_ignores_reversed_and_credits():
    truth = build_truth(ACCOUNTS, ROWS)
    assert truth["C9.ACC-1.largest_expense.2026-09.amount"] == 100.0
    assert truth["C9.ACC-1.largest_expense.2026-09.transaction_id"] == "T1"
    assert truth["C9.ACC-1.largest_expense.2026-09.merchant_name"] == "Merchant T1"


def test_last_20_is_only_the_newest_rows():
    rows = [row(f"F{n:02d}", "-1.00", "fuel", day=n) for n in range(1, 26)]
    truth = build_truth(ACCOUNTS, rows)
    assert truth["C9.ACC-1.last_20.count"] == 20
    assert truth["C9.ACC-1.last_20.spend.fuel"] == 20.0
    assert truth["C9.ACC-1.spend.fuel.all"] == 25.0


def test_seeded_truth_traps(tmp_path):
    db = tmp_path / "bank.db"
    generate(db)
    truth = build_truth(*load_rows(db))

    # Reversed 12000 is ignored.
    assert truth["C002.ACC-2001.largest_expense.2026-08.amount"] == 9850.0
    assert truth["C002.ACC-2001.largest_expense.2026-08.merchant_name"] == "Falcon Airways"

    assert truth["C001.ACC-1001.spend.fuel.all"] == truth["C001.ACC-1001.spend.fuel.2026-10"] > 0

    # "Last 20" differs from the month, so an agent that re-queries the month gets a wrong total.
    assert truth["C001.ACC-1002.last_20.spend.fuel"] > 0
    assert truth["C001.ACC-1002.last_20.spend.fuel"] != truth["C001.ACC-1002.spend.fuel.2026-10"]

    # Savings has no dining: the combined figure and the current account figure agree.
    for month in ("2026-09", "2026-10"):
        assert (
            truth[f"C001.ALL.spend.dining.{month}"] == truth[f"C001.ACC-1002.spend.dining.{month}"]
        )

    assert truth["C003.ACC-3001.transactions.count.2026-10"] == 0
    assert truth["C003.ACC-3001.transactions.count"] == 5


def test_public_cases_are_valid(tmp_path):
    from pathlib import Path

    db, truth_path = tmp_path / "bank.db", tmp_path / "truth.json"
    generate(db)
    truth_path.write_text(json.dumps(build_truth(*load_rows(db))), "utf-8")
    public = Path(__file__).resolve().parents[2] / "eval" / "cases" / "public"
    assert validate([public], truth_path) == []
    assert len(list(public.glob("*.yaml"))) == 10


def test_validate_reports_a_bad_case_and_a_missing_truth_key(tmp_path):
    truth_path = tmp_path / "truth.json"
    truth_path.write_text("{}", "utf-8")
    (tmp_path / "bad.yaml").write_text("id: X\n", "utf-8")
    (tmp_path / "missing.yaml").write_text(
        "id: Y\ndescription: d\nsession_token: t\nturns: [{user: u}]\n"
        "checks: [{type: numeric_answer, truth_key: no.such.key}]\n",
        "utf-8",
    )
    errors = validate([tmp_path], truth_path)
    assert any("bad.yaml" in error for error in errors)
    assert any("no.such.key" in error for error in errors)
