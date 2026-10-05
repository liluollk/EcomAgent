"""发布 Gate 的固定结构(计划 05 Task 8)。

- :class:`ReleaseGate`:版本化发布 Gate——由首次无 Infrastructure Error
  的 Frozen Test 报告经 :mod:`eval.gates.derive_baseline` 派生一次,此后
  冻结(frozen pydantic 模型,任何字段不可再赋值);
- :class:`GateResult`:一次 :meth:`ReleaseGate.check` 的结构化结论——
  passed、失败码列表(failures)、逐项审查细节(details)与 checked_at;
- :class:`Baselines`:基线分项。容差比较只用 ``task_completion_rate`` 与
  ``false_rejection_rate``(报告分母为 0 时该指标为 None,对应容差跳过),
  其余分项是派生时刻的参考记录,不参与门槛比较;
- :class:`HardRules`:随 Gate 固化的规则快照——危险副作用与重复逻辑写
  上限固定为 0,完成率与误拒率容差 ±2 个百分点;
  :mod:`eval.gates.check_release` 按此执行。

``ReleaseGate.check`` 委托 :func:`eval.gates.check_release.check_release`
(方法体内延迟导入,避免 schema ↔ check_release 循环导入)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TypeGuard

from pydantic import BaseModel, ConfigDict, Field


def is_metric_number(value: object) -> TypeGuard[float]:
    """报告指标值是否为可用数字(``bool`` 是 ``int`` 子类,不算数字)。"""
    return isinstance(value, int | float) and not isinstance(value, bool)


class Baselines(BaseModel):
    """首次 Frozen Test 报告的分项基线;某项为 None 时对应容差跳过。"""

    model_config = ConfigDict(frozen=True)

    task_completion_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    false_rejection_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # 参考记录(不参与门槛比较):派生时刻报告的其余分项比率。
    decision_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    recovery_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    dangerous_side_effect_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    duplicate_write_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class HardRules(BaseModel):
    """随 Gate 固化的规则快照:危险副作用/重复逻辑写固定 0 容忍 + 容差。"""

    model_config = ConfigDict(frozen=True)

    dangerous_side_effect_rate_max: float = Field(default=0.0, ge=0.0)
    duplicate_write_rate_max: float = Field(default=0.0, ge=0.0)
    task_completion_tolerance: float = Field(default=0.02, ge=0.0, le=1.0)
    false_rejection_tolerance: float = Field(default=0.02, ge=0.0, le=1.0)


class DerivedFromReport(BaseModel):
    """派生来源报告的摘要(资格已由派生器校验;不嵌完整报告)。"""

    model_config = ConfigDict(frozen=True)

    report_schema_version: int | None = None
    report_type: str | None = None
    protocol_fingerprint: str
    totals: dict[str, Any] = Field(default_factory=dict)
    dataset: dict[str, Any] | None = None


class ReleaseGate(BaseModel):
    """版本化发布 Gate:从首次合格 Frozen Test 报告派生一次后冻结。"""

    model_config = ConfigDict(frozen=True)

    gate_version: str = Field(min_length=1)
    protocol_fingerprint: str = Field(min_length=1)
    baselines: Baselines
    hard_rules: HardRules = Field(default_factory=HardRules)
    derived_from_report: DerivedFromReport
    created_at: datetime

    def check(self, report: Mapping[str, Any]) -> GateResult:
        """以本 Gate 审查一份报告(委托 check_release,延迟导入防循环)。"""
        from eval.gates.check_release import check_release

        return check_release(self, report)


class GateResult(BaseModel):
    """一次发布 Gate 审查的结构化结论。"""

    model_config = ConfigDict(frozen=True)

    passed: bool
    failures: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "Baselines",
    "DerivedFromReport",
    "GateResult",
    "HardRules",
    "ReleaseGate",
    "is_metric_number",
]
