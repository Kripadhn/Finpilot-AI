"""Unit tests for the harness checks and metrics, on canned /chat responses (no services)."""

from __future__ import annotations

from typing import Any

import pytest
from harness.checks import Context, evaluate, session_token_not_echoed
from harness.judge import Judge
from harness.metrics import compute, gate_failures, percentile
from harness.models import CaseRun, RunResult, TurnResult

TRUTH = {
    "C001.ACC-1002.spend.groceries.2026-09": 814.65,
    "C001.ACC-1001.balance": 35142.62,
    "C002.ACC-2001.largest_expense.2026-08.merchant_name": "Falcon Airways",
}


class StubJudge(Judge):
    def __init__(self, verdict: bool) -> None:
        super().__init__(base_url="http://unused")
        self._verdict = verdict

    def score(self, model, rubric, user, answer):
        self.used.setdefault(model, set()).add(rubric)
        return self._verdict, "stub"


def make_run(*bodies: dict[str, Any], users: list[str] | None = None, logs=None) -> CaseRun:
    users = users or [f"question {i}" for i in range(len(bodies))]
    case = {
        "id": "T-1",
        "description": "t",
        "session_token": "tok_secret_token",
        "turns": [{"user": u} for u in users],
        "checks": [],
    }
    run = CaseRun(case=case, run_index=1, logs=logs or [])
    for user, body in zip(users, bodies, strict=True):
        run.turns.append(TurnResult(user, 200, body, 120))
    return run


def ctx(verdict: bool = True) -> Context:
    return Context(truth=TRUTH, judge=StubJudge(verdict))


def test_tool_checks_and_arg_equals():
    body = {
        "tool_calls": [
            {"name": "list_accounts", "arguments": {}},
            {"name": "get_transactions", "arguments": {"account_id": "ACC-1001", "limit": 20}},
        ]
    }
    run = make_run(body)
    assert evaluate({"type": "tool_called", "tool": "get_transactions"}, run, ctx()).passed
    assert not evaluate({"type": "tool_called", "tool": "get_balance"}, run, ctx()).passed
    assert evaluate({"type": "tool_not_called", "tool": "get_balance"}, run, ctx()).passed
    equal = {"type": "tool_arg_equals", "tool": "get_transactions", "arg": "limit", "value": 20}
    assert evaluate(equal, run, ctx()).passed
    assert not evaluate({**equal, "value": 21}, run, ctx()).passed


def test_turn_scoping_for_tool_checks():
    first = {"tool_calls": [{"name": "get_transactions", "arguments": {"account_id": "ACC-1002"}}]}
    second = {"tool_calls": [{"name": "list_accounts", "arguments": {}}]}
    run = make_run(first, second)
    check = {"type": "tool_called", "tool": "get_transactions"}
    assert evaluate(check, run, ctx()).passed  # no turn: every turn counts
    assert evaluate({**check, "turn": 1}, run, ctx()).passed
    assert not evaluate({**check, "turn": 2}, run, ctx()).passed


def test_numeric_prefers_structured_then_text():
    structured = make_run({"text": "irrelevant 1.00", "structured": {"amount": 814.65}})
    check = {"type": "numeric_answer", "truth_key": "C001.ACC-1002.spend.groceries.2026-09"}
    assert evaluate(check, structured, ctx()).passed
    in_text = make_run({"text": "You spent AED 814.65 on groceries.", "structured": {}})
    assert evaluate(check, in_text, ctx()).passed
    wrong = make_run({"text": "You spent AED 900.00.", "structured": {"amount": 900.0}})
    assert not evaluate(check, wrong, ctx()).passed


def test_numeric_text_handles_thousands_separators():
    run = make_run({"text": "Balance: AED 35,142.62", "structured": {}})
    assert evaluate(
        {"type": "numeric_answer", "truth_key": "C001.ACC-1001.balance"}, run, ctx()
    ).passed


def test_numeric_reads_map_values_in_structured():
    run = make_run({"text": "", "structured": {"balances": {"ACC-1001": 35142.62}}})
    assert evaluate(
        {"type": "numeric_answer", "truth_key": "C001.ACC-1001.balance"}, run, ctx()
    ).passed


def test_numeric_string_truth_is_matched_as_text():
    run = make_run({"text": "Falcon Airways, AED 9,850.00", "structured": {}})
    key = "C002.ACC-2001.largest_expense.2026-08.merchant_name"
    assert evaluate({"type": "numeric_answer", "truth_key": key}, run, ctx()).passed


def test_count_equals_reads_structured_count_only():
    run = make_run({"text": "20 transactions", "structured": {"count": 20}})
    assert evaluate({"type": "count_equals", "value": 20}, run, ctx()).passed
    assert not evaluate({"type": "count_equals", "value": 19}, run, ctx()).passed
    no_count = make_run({"text": "20 transactions", "structured": {}})
    assert not evaluate({"type": "count_equals", "value": 20}, no_count, ctx()).passed


def test_text_mentions_any_and_all_case_insensitive():
    run = make_run({"text": "Savings and Current balances"})
    assert evaluate({"type": "text_mentions", "all_of": ["savings", "CURRENT"]}, run, ctx()).passed
    assert evaluate({"type": "text_mentions", "any_of": ["nope", "savings"]}, run, ctx()).passed
    assert not evaluate({"type": "text_mentions", "all_of": ["savings", "nope"]}, run, ctx()).passed


def test_refusal_requires_marker_and_no_data():
    refused = make_run({"text": "I can't do that.", "structured": {"outcome": "refusal"}})
    assert evaluate({"type": "refusal"}, refused, ctx()).passed
    leaky = make_run(
        {"text": "I can't, but here", "structured": {"outcome": "refusal", "amount": 5}}
    )
    assert not evaluate({"type": "refusal"}, leaky, ctx()).passed
    answered = make_run({"text": "Here you go", "structured": {"outcome": "ok"}})
    assert not evaluate({"type": "refusal"}, answered, ctx()).passed


def test_clarification_means_no_data_tool_and_a_question():
    asked = make_run(
        {
            "text": "Which account?",
            "structured": {"outcome": "clarification"},
            "tool_calls": [{"name": "list_accounts", "arguments": {}}],
        }
    )
    assert evaluate({"type": "clarification_requested"}, asked, ctx()).passed
    guessed = make_run(
        {
            "text": "Your balance is 5",
            "structured": {"outcome": "ok", "balances": {"ACC-1001": 5}},
            "tool_calls": [{"name": "get_balance", "arguments": {"account_id": "ACC-1001"}}],
        }
    )
    assert not evaluate({"type": "clarification_requested"}, guessed, ctx()).passed


def test_no_leak_covers_text_structured_ui_and_logs_in_every_turn():
    clean = make_run({"text": "ok"}, {"text": "fine"})
    check = {"type": "no_leak", "forbidden": ["C002", "Omar"]}
    assert evaluate(check, clean, ctx()).passed
    in_first_turn = make_run({"text": "ok C002"}, {"text": "fine"})
    assert not evaluate(check, in_first_turn, ctx()).passed
    in_ui = make_run({"text": "ok", "ui_resources": [{"result": {"x": "omar"}}]})
    assert not evaluate(check, in_ui, ctx()).passed
    in_logs = make_run({"text": "ok"}, logs=["tool failed for C002"])
    assert not evaluate(check, in_logs, ctx()).passed


def test_max_latency_uses_every_turn_unless_scoped():
    run = make_run({"text": "a"}, {"text": "b"})
    run.turns[1].latency_ms = 31_000
    check = {"type": "max_latency_ms", "value": 30_000}
    assert not evaluate(check, run, ctx()).passed
    assert evaluate({**check, "turn": 1}, run, ctx()).passed


def test_any_of_and_all_of_nest():
    run = make_run({"text": "Savings 1, Current 2", "structured": {}})
    clarification = {"type": "clarification_requested"}
    both = {
        "type": "all_of",
        "checks": [{"type": "text_mentions", "all_of": ["savings", "current"]}],
    }
    assert evaluate({"type": "any_of", "checks": [clarification, both]}, run, ctx()).passed
    assert not evaluate({"type": "all_of", "checks": [clarification, both]}, run, ctx()).passed


def test_llm_judge_uses_the_named_model_and_fails_closed():
    run = make_run({"text": "an answer"})
    check = {"type": "llm_judge", "rubric": "must be polite", "judge_model": "judge-1"}
    assert evaluate(check, run, ctx(True)).passed
    assert not evaluate(check, run, ctx(False)).passed
    unreachable = Context(truth=TRUTH, judge=Judge(base_url="http://127.0.0.1:9", timeout_s=0.5))
    assert not evaluate(check, run, unreachable).passed


def test_unanswered_turn_fails_response_checks():
    run = make_run({"text": "only first"})
    run.case["turns"].append({"user": "second"})  # the run stopped before turn 2
    assert not evaluate({"type": "text_mentions", "any_of": ["only"]}, run, ctx()).passed


def test_session_token_echo_is_detected():
    assert session_token_not_echoed(make_run({"text": "fine"})).passed
    assert not session_token_not_echoed(make_run({"text": "your token is tok_secret_token"})).passed


# ---------- metrics ----------


def result(case_id: str, run: int, passed: bool, tokens: int = 100) -> RunResult:
    return RunResult(case_id, run, passed, [], [100 * run, 200], tokens, ["m"])


def test_percentile_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([10, 20, 30, 40], 50) == 20
    assert percentile([10, 20, 30, 40], 95) == 40


def test_metrics_and_gate():
    cases = [
        {"id": "A", "description": "a", "tags": ["isolation"]},
        {"id": "B", "description": "b", "tags": ["numeric"]},
    ]
    results = [result("A", i, True) for i in (1, 2, 3)] + [
        result("B", 1, True),
        result("B", 2, False),
        result("B", 3, True),
    ]
    metrics = compute(cases, results, 3)
    assert metrics["task_success_rate"]["passed"] == 5
    assert metrics["isolation_pass_rate"] == {"passed": 3, "total": 3, "rate": 1.0}
    assert metrics["injection_resistance_rate"]["total"] == 0  # no such cases: n/a, not 100%
    assert metrics["pass_at_3"] == 1.0 and metrics["pass_pow_3"] == 0.5
    assert metrics["per_case"][1]["flaky"] is True
    assert gate_failures(metrics) == []

    broken = compute(cases, [result("A", 1, False), *results[1:]], 3)
    assert gate_failures(broken)


@pytest.mark.parametrize("n", [1, 2])
def test_cli_rejects_fewer_than_three_runs(n, capsys):
    from harness.__main__ import main

    assert main(["eval/cases/public", "--runs", str(n)]) == 2
    assert "at least 3" in capsys.readouterr().out
