"""指标汇总 — 通过率与失败分类（断言 / 超时 / 错误 / 重试）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Status = Literal["passed", "failed", "timeout", "error"]


@dataclass
class CaseRecord:
    """单个 case 的评测结论。"""

    name: str
    status: Status
    duration: float
    attempts: int = 1
    failures: list[dict] = field(default_factory=list)
    trace_path: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass
class Metrics:
    """一次运行的指标汇总。"""

    total: int
    passed: int
    failed: int
    timeout: int
    errors: int
    duration: float
    cases: list[CaseRecord] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def summary_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "timeout": self.timeout,
            "errors": self.errors,
            "pass_rate": round(self.pass_rate, 4),
            "duration_s": round(self.duration, 3),
            "cases": {c.name: {"status": c.status, "attempts": c.attempts} for c in self.cases},
        }


def summarize(records: list[CaseRecord], total_duration: float) -> Metrics:
    """按状态分类汇总 case 结论。"""
    counts = {"passed": 0, "failed": 0, "timeout": 0, "error": 0}
    for r in records:
        counts[r.status] = counts.get(r.status, 0) + 1
    return Metrics(
        total=len(records),
        passed=counts["passed"],
        failed=counts["failed"],
        timeout=counts["timeout"],
        errors=counts["error"],
        duration=total_duration,
        cases=records,
    )