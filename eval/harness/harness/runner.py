"""Runner: plays each case against POST /chat, applies chaos, captures logs, scores checks.

Runs are sequential on purpose: chaos is process-global state on the mock API, so concurrent
cases would contaminate each other. Chaos is always reset after a case, also on failure.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .checks import Context, evaluate, session_token_not_echoed
from .judge import Judge
from .models import CaseRun, CheckResult, RunResult, TurnResult

LOG_TAIL_FALLBACK = 200


@dataclass
class RunnerConfig:
    agent_url: str
    mock_api_url: str
    admin_key: str
    runs: int = 3
    chat_timeout_s: float = 180.0


class Runner:
    def __init__(self, config: RunnerConfig, ctx: Context) -> None:
        self._config = config
        self._ctx = ctx
        self._http = httpx.Client(timeout=config.chat_timeout_s)

    def close(self) -> None:
        self._http.close()

    # ---------- agent / chaos plumbing ----------

    def agent_healthy(self) -> bool:
        try:
            return self._http.get(f"{self._config.agent_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def set_chaos(self, error_rate: float, latency_ms: int) -> None:
        response = self._http.post(
            f"{self._config.mock_api_url}/_admin/chaos",
            headers={"X-Admin-Key": self._config.admin_key},
            json={"error_rate": error_rate, "latency_ms": latency_ms},
            timeout=10,
        )
        response.raise_for_status()

    def _agent_logs(self) -> list[str]:
        try:
            payload = self._http.get(f"{self._config.agent_url}/logs", timeout=10).json()
        except (httpx.HTTPError, ValueError):
            return []
        return list(payload.get("lines") or []) if payload.get("enabled") else []

    @staticmethod
    def _new_lines(before: list[str], after: list[str]) -> list[str]:
        """Log lines written during a run. The agent keeps a bounded ring, so be conservative."""
        if after[: len(before)] == before:
            return after[len(before) :]
        return after[-LOG_TAIL_FALLBACK:]

    # ---------- one run ----------

    def _play(self, case: dict[str, Any], run_index: int) -> CaseRun:
        run = CaseRun(case=case, run_index=run_index)
        conversation_id: str | None = None
        for turn in case["turns"]:
            payload: dict[str, Any] = {
                "session_token": case["session_token"],
                "message": turn["user"],
            }
            if conversation_id:
                payload["conversation_id"] = conversation_id
            started = time.perf_counter()
            try:
                response = self._http.post(f"{self._config.agent_url}/chat", json=payload)
            except httpx.HTTPError as error:
                elapsed = round((time.perf_counter() - started) * 1000)
                result = TurnResult(turn["user"], 0, {}, elapsed, type(error).__name__)
                run.turns.append(result)
                run.error = f"agent unreachable or timed out: {type(error).__name__}"
                return run
            elapsed = round((time.perf_counter() - started) * 1000)
            try:
                body = response.json() if response.status_code == 200 else {}
            except ValueError:
                body = {}
            error = None if response.status_code == 200 and body else f"HTTP {response.status_code}"
            run.turns.append(TurnResult(turn["user"], response.status_code, body, elapsed, error))
            if error:
                run.error = f"/chat failed with {error}"
                return run
            conversation_id = body.get("conversation_id") or conversation_id
        return run

    def run_case(self, case: dict[str, Any], run_index: int) -> RunResult:
        chaos = (case.get("setup") or {}).get("chaos")
        logs_before = self._agent_logs()
        try:
            if chaos:
                self.set_chaos(chaos["error_rate"], chaos["latency_ms"])
            run = self._play(case, run_index)
        except httpx.HTTPError as error:
            run = CaseRun(case=case, run_index=run_index, error=f"chaos setup failed: {error}")
        finally:
            if chaos:
                try:
                    self.set_chaos(0.0, 0)
                except httpx.HTTPError:
                    pass
        run.logs = self._new_lines(logs_before, self._agent_logs())

        checks = [evaluate(check, run, self._ctx) for check in case["checks"]]
        checks.append(session_token_not_echoed(run))
        if run.error:
            checks.append(CheckResult("implicit:run_completed", False, run.error))
        usage = [turn.usage for turn in run.turns]
        return RunResult(
            case_id=case["id"],
            run_index=run_index,
            passed=all(check.passed for check in checks),
            checks=checks,
            latencies_ms=[turn.latency_ms for turn in run.turns],
            tokens=sum(
                int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0) for u in usage
            ),
            models=sorted({str(u["model"]) for u in usage if u.get("model")}),
            error=run.error,
        )

    # ---------- everything ----------

    def run_all(
        self,
        cases: list[dict[str, Any]],
        progress: Callable[[str], None] = lambda _: None,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        self.set_chaos(0.0, 0)  # start from a clean upstream
        try:
            for case in cases:
                for run_index in range(1, self._config.runs + 1):
                    result = self.run_case(case, run_index)
                    mark = "PASS" if result.passed else "FAIL"
                    progress(f"{case['id']} run {run_index}/{self._config.runs}: {mark}")
                    results.append(result)
        finally:
            try:
                self.set_chaos(0.0, 0)
            except httpx.HTTPError:
                pass
        return results


def make_context(truth: dict[str, Any], ollama_base_url: str) -> Context:
    return Context(truth=truth, judge=Judge(base_url=ollama_base_url))
