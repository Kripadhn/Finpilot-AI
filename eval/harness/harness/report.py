"""Report writers: reports/report.json (machine) and reports/report.md (human), EV-5.

The report only states what the run measured. Metrics with no matching cases show "n/a"
rather than a made-up number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .judge import Judge
from .models import CheckResult, RunResult


def _pct(stat: dict[str, Any]) -> str:
    if not stat["total"]:
        return "n/a (no cases with this tag)"
    return f"{stat['rate'] * 100:.1f}% ({stat['passed']}/{stat['total']})"


def _num(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return f"{value}{suffix}"
    return f"{value:.2f}{suffix}"


def _failed_leaves(check: CheckResult) -> list[CheckResult]:
    if check.children:
        return [leaf for child in check.children for leaf in _failed_leaves(child)]
    return [] if check.passed else [check]


def write_reports(
    out_dir: Path,
    folder: str,
    metrics: dict[str, Any],
    results: list[RunResult],
    judge: Judge,
    gate: list[str],
    environment: dict[str, Any],
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    judge_info = {model: sorted(rubrics) for model, rubrics in judge.used.items()}
    payload = {
        "cases_folder": folder,
        "environment": environment,
        "metrics": metrics,
        "gate": {"passed": not gate, "failures": gate},
        "judge": judge_info,
        "runs": [
            {
                "case_id": r.case_id,
                "run": r.run_index,
                "passed": r.passed,
                "latencies_ms": r.latencies_ms,
                "tokens": r.tokens,
                "models": r.models,
                "error": r.error,
                "checks": [c.as_dict() for c in r.checks],
            }
            for r in results
        ],
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")

    md_path = out_dir / "report.md"
    md_path.write_text(_markdown(folder, metrics, results, judge_info, gate, environment), "utf-8")
    return json_path, md_path


def _markdown(
    folder: str,
    m: dict[str, Any],
    results: list[RunResult],
    judge_info: dict[str, list[str]],
    gate: list[str],
    environment: dict[str, Any],
) -> str:
    k = m["runs_per_case"]
    lines = [
        "# Evaluation report",
        "",
        f"- Cases folder: `{folder}` ({m['cases']} cases x {k} runs = {m['total_runs']} runs)",
        f"- Model(s) reported by the agent: {', '.join(environment['models']) or 'n/a'}",
        f"- Agent URL: `{environment['agent_url']}`",
        f"- Gate (isolation and injection resistance at 100%): "
        f"**{'PASS' if not gate else 'FAIL'}**",
    ]
    lines += [f"  - {failure}" for failure in gate]
    lines += [
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Task success rate | {_pct(m['task_success_rate'])} |",
        f"| Tool-call accuracy | {_pct(m['tool_call_accuracy'])} |",
        f"| Numeric accuracy | {_pct(m['numeric_accuracy'])} |",
        f"| Isolation pass rate | {_pct(m['isolation_pass_rate'])} |",
        f"| Injection resistance rate | {_pct(m['injection_resistance_rate'])} |",
        f"| Clarification rate | {_pct(m['clarification_rate'])} |",
        f"| Latency p50 (per turn) | {_num(m['latency_ms']['p50'], ' ms')} |",
        f"| Latency p95 (per turn) | {_num(m['latency_ms']['p95'], ' ms')} |",
        f"| Tokens per task | {_num(m['tokens_per_task'])} |",
        f"| pass@1 (mean per-case pass proportion) | {_num(m['pass_at_1'])} |",
        f"| pass@{k} (at least one pass in {k} runs) | {_num(m[f'pass_at_{k}'])} |",
        f"| pass^{k} (all {k} runs pass) | {_num(m[f'pass_pow_{k}'])} |",
        f"| Mean per-case variance p(1-p) | {_num(m['mean_case_variance'])} |",
        "",
        "Definitions are in `eval/harness/harness/metrics.py`. Latency is wall-clock per "
        "`/chat` turn measured by the harness. Tokens are what the agent reports; a run that "
        "used the deterministic fallback or replay provider reports the provider's counts.",
        "",
        "## Per case",
        "",
        "| Case | Tags | Passed | Variance | Description |",
        "|---|---|---|---|---|",
    ]
    for case in m["per_case"]:
        flaky = " (flaky)" if case["flaky"] else ""
        lines.append(
            f"| {case['id']} | {', '.join(case['tags'])} | {case['passes']}/{case['runs']}{flaky} "
            f"| {case['variance']:.2f} | {case['description']} |"
        )

    failures = [r for r in results if not r.passed]
    lines += ["", "## Failures", ""]
    if not failures:
        lines.append("No failing runs.")
    for result in failures:
        lines.append(f"### {result.case_id} run {result.run_index}")
        if result.error:
            lines.append(f"- run error: {result.error}")
        for check in result.checks:
            for leaf in _failed_leaves(check):
                lines.append(f"- `{leaf.type}`: {leaf.detail}")
        lines.append("")

    lines += ["## LLM judge", ""]
    if judge_info:
        for model, rubrics in judge_info.items():
            lines.append(f"- Judge model `{model}`; rubrics:")
            lines += [f"  - {rubric}" for rubric in rubrics]
    else:
        lines.append("No `llm_judge` checks ran. All checks were deterministic.")
    return "\n".join(lines) + "\n"
