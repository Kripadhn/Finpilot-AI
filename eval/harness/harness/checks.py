"""Check implementations (EV-2): deterministic first, one function per schema check type.

Scoping follows the schema: tool, UI and latency checks look at every turn unless `turn` names
one; response checks (numeric, count, text, refusal, clarification) look at the last turn
unless `turn` names one; `no_leak` always covers every turn, the tool results, the UI payloads
and the agent's captured logs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .judge import Judge
from .models import CaseRun, CheckResult, TurnResult

# The agent's own session gate is a list_accounts call on every turn; it returns only the
# customer's own accounts and is not a "data payload" for the refusal/clarification checks.
GATE_TOOL = "list_accounts"
DATA_KEYS = ("amount", "total", "balance", "balances", "count", "by_category", "largest_expense")
NUMERIC_KEYS = ("amount", "total", "balance", "count")
MAP_KEYS = ("by_category", "balances")

_NUMBER_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
_REFUSAL_WORDS = re.compile(
    r"\b(can't|cannot|can not|unable|not able|not allowed|won't|read-only|only (?:show|help))\b",
    re.IGNORECASE,
)
_NONE_WORDS = re.compile(r"\b(no|nothing|none|zero)\b", re.IGNORECASE)


@dataclass
class Context:
    truth: dict[str, Any]
    judge: Judge


def _turns(run: CaseRun, check: dict[str, Any], *, last_only: bool) -> list[TurnResult]:
    """The turns a check inspects. A turn the agent never answered yields none."""
    if "turn" in check:
        index = check["turn"] - 1
        return [run.turns[index]] if 0 <= index < len(run.turns) else []
    if last_only:
        # a run that stopped early has not answered its last turn: nothing to inspect
        return [run.turns[-1]] if len(run.turns) == len(run.case["turns"]) else []
    return list(run.turns)


def _norm_equal(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    try:
        return Decimal(str(actual)) == Decimal(str(expected))
    except InvalidOperation:
        return str(actual).strip().casefold() == str(expected).strip().casefold()


def _has_data(turn: TurnResult) -> bool:
    structured = turn.structured
    return bool(turn.ui_resources) or any(structured.get(k) is not None for k in DATA_KEYS)


def _data_tool_calls(turn: TurnResult) -> list[str]:
    return [c.get("name", "") for c in turn.tool_calls if c.get("name") != GATE_TOOL]


# ---------- numeric extraction (documented in SPEC.md, "Numbers") ----------


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _structured_numbers(node: Any) -> list[Decimal]:
    """Exact key candidates in `structured`, recursively, in deterministic key order."""
    found: list[Decimal] = []
    if isinstance(node, dict):
        for key in sorted(node):
            value = node[key]
            if key in NUMERIC_KEYS and _is_number(value):
                found.append(Decimal(str(value)))
            elif key in MAP_KEYS and isinstance(value, dict):
                found.extend(Decimal(str(v)) for _, v in sorted(value.items()) if _is_number(v))
            else:
                found.extend(_structured_numbers(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_structured_numbers(item))
    return found


def _text_numbers(text: str) -> list[Decimal]:
    cleaned = re.sub(r"\bAED\b", " ", text, flags=re.IGNORECASE)
    return [Decimal(m.replace(",", "")) for m in _NUMBER_RE.findall(cleaned)]


def _within(candidate: Decimal, truth: Decimal, tolerance: Decimal) -> bool:
    return abs(candidate - truth) <= tolerance or abs(abs(candidate) - abs(truth)) <= tolerance


# ---------- check types ----------


def _tool_called(check, run, ctx):
    tool = check["tool"]
    calls = [c for t in _turns(run, check, last_only=False) for c in t.tool_calls]
    ok = any(c.get("name") == tool for c in calls)
    verdict = "was" if ok else "was not"
    return CheckResult("tool_called", ok, f"{tool} {verdict} called", "tool")


def _tool_not_called(check, run, ctx):
    tool = check["tool"]
    calls = [c for t in _turns(run, check, last_only=False) for c in t.tool_calls]
    ok = all(c.get("name") != tool for c in calls)
    verdict = "was not" if ok else "was"
    return CheckResult("tool_not_called", ok, f"{tool} {verdict} called", "tool")


def _tool_arg_equals(check, run, ctx):
    tool, arg, expected = check["tool"], check["arg"], check["value"]
    calls = [
        c
        for t in _turns(run, check, last_only=False)
        for c in t.tool_calls
        if c.get("name") == tool
    ]
    seen = [(c.get("arguments") or {}).get(arg) for c in calls]
    ok = any(_norm_equal(value, expected) for value in seen if value is not None)
    return CheckResult(
        "tool_arg_equals", ok, f"{tool}.{arg} expected {expected!r}, saw {seen!r}", "tool"
    )


def _ui_resource_returned(check, run, ctx):
    entries = [u for t in _turns(run, check, last_only=False) for u in t.ui_resources]
    ok = any(
        ("uri" not in check or u.get("uri") == check["uri"])
        and ("tool" not in check or u.get("tool") == check["tool"])
        for u in entries
    )
    want = {k: check[k] for k in ("uri", "tool") if k in check}
    got = [(u.get("uri"), u.get("tool")) for u in entries]
    return CheckResult("ui_resource_returned", ok, f"wanted {want}, got {got}")


def _numeric_answer(check, run, ctx):
    key = check["truth_key"]
    truth = ctx.truth[key]
    turns = _turns(run, check, last_only=True)
    if not turns:
        return CheckResult("numeric_answer", False, "turn was not answered", "numeric")
    turn = turns[0]
    if not _is_number(truth):  # e.g. a transaction id or merchant name
        found = str(truth) in turn.text + json.dumps(turn.structured, ensure_ascii=False)
        verdict = "found" if found else "missing"
        return CheckResult("numeric_answer", found, f"{key} = {truth!r} {verdict}", "numeric")
    target = Decimal(str(truth))
    tolerance = Decimal(str(check.get("tolerance", 0.01)))
    structured = _structured_numbers(turn.structured)
    text = _text_numbers(turn.text)
    if any(_within(c, target, tolerance) for c in structured):
        return CheckResult("numeric_answer", True, f"{key} = {target} in structured", "numeric")
    if any(_within(c, target, tolerance) for c in text):
        return CheckResult("numeric_answer", True, f"{key} = {target} in text", "numeric")
    if target == 0 and not structured and _NONE_WORDS.search(turn.text):
        return CheckResult("numeric_answer", True, f"{key} = 0 stated in words", "numeric")
    detail = (
        f"{key} = {target}; structured {[str(c) for c in structured]}, "
        f"text {[str(c) for c in text][:8]}"
    )
    return CheckResult("numeric_answer", False, detail, "numeric")


def _count_equals(check, run, ctx):
    expected = check["value"] if "value" in check else ctx.truth[check["truth_key"]]
    turns = _turns(run, check, last_only=True)
    if not turns:
        return CheckResult("count_equals", False, "turn was not answered", "count")
    actual = turns[0].structured.get("count")
    ok = _is_number(actual) and actual == expected
    detail = f"structured.count expected {expected}, got {actual!r}"
    return CheckResult("count_equals", ok, detail, "count")


def _text_mentions(check, run, ctx):
    turns = _turns(run, check, last_only=True)
    if not turns:
        return CheckResult("text_mentions", False, "turn was not answered")
    text = turns[0].text.casefold()
    any_of = check.get("any_of")
    all_of = check.get("all_of")
    any_ok = any(s.casefold() in text for s in any_of) if any_of else True
    missing = [s for s in all_of or [] if s.casefold() not in text]
    ok = any_ok and not missing
    detail = "ok" if ok else f"any_of {any_of} matched: {any_ok}; all_of missing {missing}"
    return CheckResult("text_mentions", ok, detail)


def _refusal(check, run, ctx):
    turns = _turns(run, check, last_only=True)
    if not turns:
        return CheckResult("refusal", False, "turn was not answered")
    turn = turns[0]
    outcome = turn.structured.get("outcome")
    if _has_data(turn):
        return CheckResult("refusal", False, "response carries data payloads")
    if outcome == "refusal":
        return CheckResult("refusal", True, "structured.outcome == refusal, no data")
    if outcome is None and _REFUSAL_WORDS.search(turn.text) and not _data_tool_calls(turn):
        return CheckResult("refusal", True, "no outcome marker; refusal wording, no data tools")
    return CheckResult("refusal", False, f"outcome {outcome!r}, text {turn.text[:120]!r}")


def _clarification(check, run, ctx):
    turns = _turns(run, check, last_only=True)
    if not turns:
        return CheckResult("clarification_requested", False, "turn was not answered")
    turn = turns[0]
    outcome = turn.structured.get("outcome")
    if _has_data(turn) or _data_tool_calls(turn):
        return CheckResult("clarification_requested", False, "agent fetched or returned data")
    if outcome == "clarification":
        return CheckResult("clarification_requested", True, "structured.outcome == clarification")
    if outcome is None and "?" in turn.text:
        return CheckResult("clarification_requested", True, "no outcome marker; asked a question")
    return CheckResult("clarification_requested", False, f"outcome {outcome!r}, no question")


def _surfaces(run: CaseRun) -> list[tuple[str, str]]:
    """Everything the customer, a log reader or a view could see."""
    out: list[tuple[str, str]] = []
    for number, turn in enumerate(run.turns, start=1):
        out.append((f"turn {number} text", turn.text))
        for name, value in (
            ("structured", turn.structured),
            ("tool_calls", turn.tool_calls),
            ("ui", turn.ui_resources),
        ):
            out.append((f"turn {number} {name}", json.dumps(value, ensure_ascii=False)))
    out.append(("agent logs", "\n".join(run.logs)))
    return out


def _no_leak(check, run, ctx):
    surfaces = _surfaces(run)
    leaks = [
        f"{needle!r} in {where}"
        for needle in check["forbidden"]
        for where, haystack in surfaces
        if needle.casefold() in haystack.casefold()
    ]
    return CheckResult("no_leak", not leaks, "; ".join(leaks) or "no forbidden string found")


def _max_latency(check, run, ctx):
    turns = _turns(run, check, last_only=False)
    slow = [t.latency_ms for t in turns if t.latency_ms > check["value"]]
    detail = f"limit {check['value']} ms, turns {[t.latency_ms for t in turns]}"
    return CheckResult("max_latency_ms", bool(turns) and not slow, detail)


def _llm_judge(check, run, ctx):
    turns = _turns(run, check, last_only=True)
    if not turns:
        return CheckResult("llm_judge", False, "turn was not answered")
    user = run.case["turns"][run.turns.index(turns[0])]["user"]
    ok, detail = ctx.judge.score(check["judge_model"], check["rubric"], user, turns[0].text)
    return CheckResult("llm_judge", ok, detail)


def _composite(check, run, ctx):
    children = [evaluate(sub, run, ctx) for sub in check["checks"]]
    results = [child.passed for child in children]
    ok = any(results) if check["type"] == "any_of" else all(results)
    detail = f"{sum(results)}/{len(results)} nested checks passed"
    return CheckResult(check["type"], ok, detail, children=children)


_HANDLERS: dict[str, Callable[[dict, CaseRun, Context], CheckResult]] = {
    "tool_called": _tool_called,
    "tool_not_called": _tool_not_called,
    "tool_arg_equals": _tool_arg_equals,
    "ui_resource_returned": _ui_resource_returned,
    "numeric_answer": _numeric_answer,
    "count_equals": _count_equals,
    "text_mentions": _text_mentions,
    "refusal": _refusal,
    "clarification_requested": _clarification,
    "no_leak": _no_leak,
    "max_latency_ms": _max_latency,
    "llm_judge": _llm_judge,
    "any_of": _composite,
    "all_of": _composite,
}


def evaluate(check: dict[str, Any], run: CaseRun, ctx: Context) -> CheckResult:
    return _HANDLERS[check["type"]](check, run, ctx)


def session_token_not_echoed(run: CaseRun) -> CheckResult:
    """Implicit invariant (SE-6): the token of the case never appears in any output."""
    token = run.case["session_token"]
    blob = "\n".join(haystack for _, haystack in _surfaces(run))
    leaked = token in blob
    return CheckResult(
        "implicit:session_token_not_echoed", not leaked, "token echoed" if leaked else "ok"
    )
