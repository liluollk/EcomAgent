"""发布 Gate 的固定结构。

- :class:`ReleaseGate`:版本化发布 Gate——由首次无 Infrastructure Error
  的 Core 报告经 :mod:`eval.gates.derive_baseline` 派生一次,此后
  冻结(frozen pydantic 模型,任何字段不可再赋值);
- :class:`GateResult`:一次 :meth:`ReleaseGate.check` 的结构化结论;
- :class:`Baselines`:Core 任务完成率基线;
- :class:`HardRules`:任务完成率回归容差;
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
    """首次 Core 报告中的任务完成率基线。"""

    model_config = ConfigDict(frozen=True)

    task_completion_rate: float = Field(ge=0.0, le=1.0)


class HardRules(BaseModel):
    """随 Gate 固化的硬规则快照。"""

    model_config = ConfigDict(frozen=True)

    task_completion_tolerance: float = Field(default=0.02, ge=0.0, le=1.0)
    """Core 任务完成率允许的最大回退(百分点比例)。"""

    require_attestations: bool = True
    """是否要求报告明确声明 Runtime 确定性套件与 E2E 通过。

    默认要求:报告未声明即"无法验证",按失败处理——缺失不能等价于通过。
    只有明确只想做完成率回归检查的 Gate 才把它设为 ``False``。
    """

    require_safety_evidence: bool = True
    """是否要求发布决策附带独立的 Safety 分区结果。

    默认要求:Core 分区没有安全适用用例(``safety_applicable_cases == 0``),
    只凭 Core 报告无法证明"危险操作被拦住"。此时必须同时提供 Safety 报告,
    否则按失败处理(``SAFETY_NOT_ATTESTED``)。把它设为 ``False`` 等于承认
    本次放行不做安全验证——只有在明确知情时才这么配。
    """


class DerivedFromReport(BaseModel):
    """派生来源报告的摘要(资格已由派生器校验;不嵌完整报告)。"""

    model_config = ConfigDict(frozen=True)

    report_schema_version: int | None = None
    report_type: str | None = None
    benchmark_fingerprint: str
    totals: dict[str, Any] = Field(default_factory=dict)
    dataset: dict[str, Any] | None = None
    known_failures: tuple[str, ...] = ()
    """派生时被显式接受的失败用例(来源报告里未通过的 case_id)。

    默认派生要求来源报告全部通过;只有 ``allow_known_failures`` 才允许冻结
    带失败的基线,并把例外项记在这里,避免"静默把缺陷固化成底线"。
    """


class ReleaseGate(BaseModel):
    """版本化发布 Gate:从首次合格 Core 报告派生一次后冻结。"""

    model_config = ConfigDict(frozen=True)

    gate_version: str = Field(min_length=1)
    benchmark_fingerprint: str = Field(min_length=1)
    baselines: Baselines
    hard_rules: HardRules = Field(default_factory=HardRules)
    case_outcomes: dict[str, bool] = Field(default_factory=dict)
    """基线逐例结果(``case_id`` → 全部重复是否都通过)。

    允许平均分掩盖单例退化:基线里通过、候选报告里失败的用例必须单独报出来,
    不能被别处的改善抵消。
    """

    case_passes: dict[str, int] = Field(default_factory=dict)
    """基线逐例通过次数(``case_id`` → 通过的重复次数)。

    ``case_outcomes`` 只记"是否 3 次全过",一个已知失败从 2/3 恶化到 1/3 仍
    是 ``False``,不会被看见。这里保留次数,让已知失败的恶化也能被门禁发现。
    """

    repetitions: int | None = None
    """基线报告的重复次数(逐例结果完整性校验用)。"""

    scenario_count: int | None = None
    """基线报告的场景数(逐例结果完整性校验用)。"""

    derived_from_report: DerivedFromReport
    created_at: datetime

    def check(
        self,
        report: Mapping[str, Any],
        *,
        safety_report: Mapping[str, Any] | None = None,
    ) -> GateResult:
        """以本 Gate 审查一份报告(委托 check_release,延迟导入防循环)。"""
        from eval.gates.check_release import check_release

        return check_release(self, report, safety_report=safety_report)


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
