"""Plain data carried between the runner, the checks and the report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnResult:
    user: str
    status: int  # HTTP status of POST /chat; 0 when the agent was unreachable
    body: dict[str, Any]  # parsed /chat response; {} when there is none
    latency_ms: int  # wall-clock, measured by the harness
    error: str | None = None

    @property
    def text(self) -> str:
        return str(self.body.get("text") or "")

    @property
    def structured(self) -> dict[str, Any]:
        value = self.body.get("structured")
        return value if isinstance(value, dict) else {}

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.body.get("tool_calls") or [] if isinstance(c, dict)]

    @property
    def ui_resources(self) -> list[dict[str, Any]]:
        return [u for u in self.body.get("ui_resources") or [] if isinstance(u, dict)]

    @property
    def usage(self) -> dict[str, Any]:
        value = self.body.get("usage")
        return value if isinstance(value, dict) else {}


@dataclass
class CaseRun:
    """One execution of one case: every turn, plus the agent's safe logs for the run."""

    case: dict[str, Any]
    run_index: int
    turns: list[TurnResult] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: str | None = None  # setup/transport failure that stopped the run


@dataclass
class CheckResult:
    type: str
    passed: bool
    detail: str
    category: str = "other"  # tool | numeric | count | other, feeds the metrics
    children: list[CheckResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "passed": self.passed, "detail": self.detail}
        if self.children:
            out["children"] = [child.as_dict() for child in self.children]
        return out


@dataclass
class RunResult:
    case_id: str
    run_index: int
    passed: bool
    checks: list[CheckResult]
    latencies_ms: list[int]
    tokens: int
    models: list[str]
    error: str | None = None
