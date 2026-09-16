"""指标汇总 — 通过率与失败分类（断言 / 超时 / 错误 / 重试）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Status = Literal["passed", "failed", "timeout", "error", "not_exercised"]

_STATUSES = ("passed", "failed", "timeout", "error", "not_exercised")


def _group_by(cases: list["CaseRecord"], key) -> dict[str, dict[str, int]]:
    """按 key(case) 分组统计各状态计数（保留未触发场景的独立计数）。"""
    grouped: dict[str, dict[str, int]] = {}
    for case in cases:
        counts = grouped.setdefault(
            key(case), {"cases": 0, "exercised": 0, **{s: 0 for s in _STATUSES}},
        )
        counts["cases"] += 1
        counts[case.status] += 1
        if case.exercised:
            counts["exercised"] += 1
    return grouped


@dataclass
class CaseRecord:
    """单个 case 的评测结论。"""

    name: str
    status: Status
    duration: float
    attempts: int = 1
    failures: list[dict] = field(default_factory=list)
    trace_path: Optional[str] = None
    kind: str = "gold"
    metrics: list[str] = field(default_factory=list)
    platform: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def exercised(self) -> bool:
        """契约是否被触发（not_exercised 不计入失败也不计入通过率分母）。"""
        return self.status != "not_exercised"


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

    @property
    def by_kind(self) -> dict[str, dict[str, int]]:
        """按场景类型汇总，保留未触发场景的独立计数。"""
        return _group_by(self.cases, lambda c: c.kind)

    @property
    def by_platform(self) -> dict[str, dict[str, int]]:
        """按平台汇总（both 表示一条场景横跨两个平台）。

        调价闭环的契约按平台分层：同一份领域命令在淘宝与抖店上的协议形态、
        错误码、价格精度都不同，只看总体通过率会掩盖单平台的退化。
        """
        return _group_by(self.cases, lambda c: c.platform or "unknown")

    @property
    def metric_hits(self) -> dict[str, dict[str, int]]:
        """按领域指标汇总各状态，指标只来自场景元数据。"""
        hits: dict[str, dict[str, int]] = {}
        for case in self.cases:
            for metric in case.metrics:
                counts = hits.setdefault(
                    metric,
                    {"cases": 0, "exercised": 0, **{status: 0 for status in _STATUSES}},
                )
                counts["cases"] += 1
                counts[case.status] += 1
                if case.exercised:
                    counts["exercised"] += 1
        return hits

    def summary_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "timeout": self.timeout,
            "errors": self.errors,
            "pass_rate": round(self.pass_rate, 4),
            "duration_s": round(self.duration, 3),
            "by_kind": self.by_kind,
            "by_platform": self.by_platform,
            "metric_hits": self.metric_hits,
            "cases": {
                c.name: {
                    "status": c.status,
                    "attempts": c.attempts,
                    "kind": c.kind,
                    "platform": c.platform,
                    "metrics": c.metrics,
                }
                for c in self.cases
            },
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


def summarize_real_rows(rows: list[dict]) -> dict:
    """汇总真实模型模式的重复运行结果。

    真实模型模式保留触发率语义：not_exercised 不当作失败；guarded 场景
    在重复运行中始终未触发时，额外标为 safe_no_op，避免把安全空操作
    误报成已验证的拦截。
    """
    by_kind: dict[str, dict[str, int]] = {}
    metric_hits: dict[str, dict[str, int]] = {}
    for row in rows:
        kind = row.get("kind", "gold")
        counts = by_kind.setdefault(
            kind,
            {"scenarios": 0, "runs": 0, "passes": 0, "triggered": 0,
             "not_exercised": 0, "safe_no_op": 0},
        )
        triggers = int(row.get("triggers", 0))
        not_exercised = int(row.get("not_exercised", 0))
        counts["scenarios"] += 1
        counts["runs"] += triggers + not_exercised
        counts["passes"] += int(row.get("passes", 0))
        counts["triggered"] += triggers
        counts["not_exercised"] += not_exercised
        if kind == "guarded" and triggers == 0:
            counts["safe_no_op"] += 1

        for metric in row.get("metrics", []):
            metric_counts = metric_hits.setdefault(
                metric,
                {"runs": 0, "triggered": 0, "passed": 0,
                 "not_exercised": 0, "safe_no_op": 0},
            )
            metric_counts["runs"] += triggers + not_exercised
            metric_counts["triggered"] += triggers
            metric_counts["passed"] += int(row.get("passes", 0))
            metric_counts["not_exercised"] += not_exercised
            if kind == "guarded" and triggers == 0:
                metric_counts["safe_no_op"] += 1
    return {"by_kind": by_kind, "metric_hits": metric_hits}
