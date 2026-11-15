"""CLI: python -m harness <cases-folder> [--runs N] (invoked by `make eval CASES=<folder>`)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bank_shared.config import settings

from .cases import REPO_ROOT, CaseError, load_cases, load_truth
from .metrics import compute, gate_failures
from .report import write_reports
from .runner import Runner, RunnerConfig, make_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an evaluation case folder against /chat")
    parser.add_argument("folder", type=Path, help="folder of schema-valid YAML cases")
    parser.add_argument(
        "--runs", type=int, default=int(os.getenv("EVAL_RUNS", "3")), help="runs per case (>= 3)"
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports")
    parser.add_argument("--agent-url", default=os.getenv("AGENT_URL"))
    parser.add_argument(
        "--no-gate", action="store_true", help="do not fail the exit code on gate failures"
    )
    args = parser.parse_args(argv)

    config = settings()
    if args.runs < 3:
        print("error: every case must run at least 3 times (EV-4); use --runs 3 or more")
        return 2

    try:
        truth = load_truth()
        cases = load_cases(args.folder, truth)
    except CaseError as error:
        print(f"error: {error}")
        return 2

    agent_url = args.agent_url or f"http://localhost:{config.agent_port}"
    runner = Runner(
        RunnerConfig(
            agent_url=agent_url,
            mock_api_url=config.mock_api_url,
            admin_key=config.mock_admin_key,
            runs=args.runs,
        ),
        make_context(truth, config.ollama_base_url),
    )
    try:
        if not runner.agent_healthy():
            print(f"error: the agent is not reachable at {agent_url}. Start it with `make up`.")
            return 2
        print(f"Running {len(cases)} cases x {args.runs} runs from {args.folder}")
        results = runner.run_all(cases, progress=print)
        metrics = compute(cases, results, args.runs)
        gate = gate_failures(metrics)
        environment = {
            "agent_url": agent_url,
            "models": sorted({m for r in results for m in r.models}),
        }
        json_path, md_path = write_reports(
            args.out,
            str(args.folder),
            metrics,
            results,
            runner._ctx.judge,
            gate,
            environment,
        )
    finally:
        runner.close()

    rate = metrics["task_success_rate"]
    print(f"\nTask success: {rate['passed']}/{rate['total']} runs")
    print(f"Reports: {json_path}  {md_path}")
    for failure in gate:
        print(f"GATE FAILED - {failure}")
    return 1 if gate and not args.no_gate else 0


if __name__ == "__main__":
    sys.exit(main())
