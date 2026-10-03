"""RunLimits 与 LoopBudget:Agent Loop 的硬限制预算(计划 02 Task 9;架构文档 §8)。

每个 AgentVersion 配置硬限制:最大模型轮次、最大 ToolCall 数、单次 Tool 超时、
Run 总时限、模型重试次数、Reconciliation 次数。达到限制时 Run 以明确错误码
失败,不能继续自动循环。

- :class:`RunLimits` 冻结值对象,带保守默认值;``from_definition`` 从
  AgentDefinition 的 ``loop_limits`` 组装视图读取覆盖,非法/非正值回落默认
  (fail closed,不放宽);
- :class:`LoopBudget` 是一次推进内的记账器:模型轮次与 ToolCall 数向上计数,
  Run Deadline 与单 Tool 超时按注入时钟判定;超限抛
  :class:`LoopLimitExceeded`(携带确定性 ``failure_code``)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final


LOOP_LIMIT_EXCEEDED: Final = "LOOP_LIMIT_EXCEEDED"
"""模型轮次超过上限的确定错误码。"""

TOOL_CALL_LIMIT_EXCEEDED: Final = "TOOL_CALL_LIMIT_EXCEEDED"
"""ToolCall 数超过上限的确定错误码。"""

TOOL_TIMEOUT: Final = "TOOL_TIMEOUT"
"""单次 Tool 调用超过时限的确定错误码。"""

RUN_DEADLINE_EXCEEDED: Final = "RUN_DEADLINE_EXCEEDED"
"""Run 总时限超过的确定错误码。"""


class LoopLimitExceeded(Exception):
    """Agent Loop 预算超限;``failure_code`` 是写入 Run 的确定错误码。"""

    def __init__(self, failure_code: str, message: str | None = None) -> None:
        self.failure_code = failure_code
        super().__init__(message or f"loop limit exceeded: {failure_code}")


def _positive_int(overrides: Mapping[str, Any], key: str, default: int) -> int:
    value = overrides.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _positive_float(overrides: Mapping[str, Any], key: str, default: float) -> float:
    value = overrides.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


@dataclass(frozen=True)
class RunLimits:
    """AgentVersion 硬限制;默认值为保守上限,防止无界循环。"""

    max_model_rounds: int = 25
    max_tool_calls: int = 50
    tool_timeout_seconds: float = 120.0
    run_deadline_seconds: float = 900.0
    backend_retries: int = 2
    reconcile_budget: int = 3

    @classmethod
    def from_definition(cls, loop_limits: Mapping[str, Any] | None) -> RunLimits:
        """从 AgentDefinition 的 ``loop_limits`` 组装;非法覆盖回落保守默认。"""
        overrides = loop_limits or {}
        return cls(
            max_model_rounds=_positive_int(overrides, "max_model_rounds", 25),
            max_tool_calls=_positive_int(overrides, "max_tool_calls", 50),
            tool_timeout_seconds=_positive_float(
                overrides, "tool_timeout_seconds", 120.0
            ),
            run_deadline_seconds=_positive_float(
                overrides, "run_deadline_seconds", 900.0
            ),
            backend_retries=_positive_int(overrides, "backend_retries", 2),
            reconcile_budget=_positive_int(overrides, "reconcile_budget", 3),
        )


@dataclass
class LoopBudget:
    """一次推进内的确定性预算记账器。

    ``model_rounds_used`` / ``tool_calls_used`` 由调用方以 Run 上已持久化的
    计数初始化(模型轮次按历史 Assistant Message 计,ToolCall 数按已有
    ToolCall 行计),使恢复重入沿用同一预算;``deadline_at`` 显式给定 Run
    总时限(缺省为 ``started_at + run_deadline_seconds``)。
    """

    limits: RunLimits
    model_rounds_used: int = 0
    tool_calls_used: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    deadline_at: datetime | None = None

    @property
    def run_deadline(self) -> datetime:
        if self.deadline_at is not None:
            return self.deadline_at
        return self.started_at + timedelta(seconds=self.limits.run_deadline_seconds)

    @property
    def backend_retries(self) -> int:
        return self.limits.backend_retries

    @property
    def reconcile_budget(self) -> int:
        return self.limits.reconcile_budget

    def record_model_round(self) -> None:
        """记一次模型轮次;超过上限抛 ``LOOP_LIMIT_EXCEEDED``。"""
        self.model_rounds_used += 1
        if self.model_rounds_used > self.limits.max_model_rounds:
            raise LoopLimitExceeded(
                LOOP_LIMIT_EXCEEDED,
                f"model rounds {self.model_rounds_used} exceed limit "
                f"{self.limits.max_model_rounds}",
            )

    def record_tool_call(self) -> None:
        """记一次 ToolCall 处理;超过上限抛 ``TOOL_CALL_LIMIT_EXCEEDED``。"""
        self.tool_calls_used += 1
        if self.tool_calls_used > self.limits.max_tool_calls:
            raise LoopLimitExceeded(
                TOOL_CALL_LIMIT_EXCEEDED,
                f"tool calls {self.tool_calls_used} exceed limit "
                f"{self.limits.max_tool_calls}",
            )

    def ensure_run_deadline(self, *, now: datetime) -> None:
        """Run 总时限检查;超过抛 ``RUN_DEADLINE_EXCEEDED``。"""
        if now > self.run_deadline:
            raise LoopLimitExceeded(
                RUN_DEADLINE_EXCEEDED,
                f"run deadline {self.run_deadline.isoformat()} exceeded at "
                f"{now.isoformat()}",
            )

    def tool_deadline(self, *, now: datetime) -> datetime:
        """单次 Tool 调用的截止时刻:不超过 Run 总时限。"""
        by_tool = now + timedelta(seconds=self.limits.tool_timeout_seconds)
        return min(by_tool, self.run_deadline)

    def ensure_tool_timeout(self, *, started_at: datetime, now: datetime) -> None:
        """单 Tool 超时检查;超过抛 ``TOOL_TIMEOUT``。"""
        deadline = self.tool_deadline(now=started_at)
        if now > deadline:
            raise LoopLimitExceeded(
                TOOL_TIMEOUT,
                f"tool call started at {started_at.isoformat()} exceeded deadline "
                f"{deadline.isoformat()}",
            )


__all__ = [
    "LOOP_LIMIT_EXCEEDED",
    "RUN_DEADLINE_EXCEEDED",
    "TOOL_CALL_LIMIT_EXCEEDED",
    "TOOL_TIMEOUT",
    "LoopBudget",
    "LoopLimitExceeded",
    "RunLimits",
]
