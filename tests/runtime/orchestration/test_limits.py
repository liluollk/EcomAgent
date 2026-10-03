"""RunLimits 与 LoopBudget 的确定性预算记账测试(计划 02 Task 9)。

纯内存单元测试:超限判定必须确定(同一输入永远同一结论),
错误码与计划/架构文档一致。
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from runtime.orchestration.limits import LoopBudget, LoopLimitExceeded, RunLimits


START = datetime(2026, 1, 1, tzinfo=UTC)


def test_default_limits_are_conservative() -> None:
    """默认值必须全部为正的保守上限,防止无界循环。"""
    limits = RunLimits()
    assert limits.max_model_rounds > 0
    assert limits.max_tool_calls > 0
    assert limits.tool_timeout_seconds > 0
    assert limits.run_deadline_seconds > 0
    assert limits.backend_retries > 0
    assert limits.reconcile_budget > 0


def test_run_limits_are_frozen() -> None:
    limits = RunLimits()
    with pytest.raises(FrozenInstanceError):
        limits.max_model_rounds = 999  # type: ignore[misc]


def test_model_round_limit_exceeded_is_deterministic() -> None:
    budget = LoopBudget(RunLimits(max_model_rounds=2))
    budget.record_model_round()
    budget.record_model_round()
    with pytest.raises(LoopLimitExceeded) as first:
        budget.record_model_round()
    with pytest.raises(LoopLimitExceeded) as second:
        budget.record_model_round()
    assert first.value.failure_code == "LOOP_LIMIT_EXCEEDED"
    assert second.value.failure_code == "LOOP_LIMIT_EXCEEDED"


def test_tool_call_limit_exceeded() -> None:
    budget = LoopBudget(RunLimits(max_tool_calls=1))
    budget.record_tool_call()
    with pytest.raises(LoopLimitExceeded) as exc:
        budget.record_tool_call()
    assert exc.value.failure_code == "TOOL_CALL_LIMIT_EXCEEDED"


def test_run_deadline_exceeded() -> None:
    budget = LoopBudget(
        RunLimits(run_deadline_seconds=60), model_rounds_used=0, started_at=START
    )
    budget.ensure_run_deadline(now=START + timedelta(seconds=59))
    with pytest.raises(LoopLimitExceeded) as exc:
        budget.ensure_run_deadline(now=START + timedelta(seconds=61))
    assert exc.value.failure_code == "RUN_DEADLINE_EXCEEDED"


def test_explicit_run_deadline_wins_over_started_at() -> None:
    deadline = START + timedelta(seconds=10)
    budget = LoopBudget(
        RunLimits(run_deadline_seconds=3600),
        model_rounds_used=0,
        started_at=START,
        deadline_at=deadline,
    )
    budget.ensure_run_deadline(now=deadline - timedelta(seconds=1))
    with pytest.raises(LoopLimitExceeded) as exc:
        budget.ensure_run_deadline(now=deadline + timedelta(seconds=1))
    assert exc.value.failure_code == "RUN_DEADLINE_EXCEEDED"


def test_tool_timeout_exceeded() -> None:
    budget = LoopBudget(RunLimits(tool_timeout_seconds=30), started_at=START)
    budget.ensure_tool_timeout(
        started_at=START, now=START + timedelta(seconds=29)
    )
    with pytest.raises(LoopLimitExceeded) as exc:
        budget.ensure_tool_timeout(started_at=START, now=START + timedelta(seconds=31))
    assert exc.value.failure_code == "TOOL_TIMEOUT"


def test_tool_deadline_bounded_by_run_deadline() -> None:
    budget = LoopBudget(
        RunLimits(tool_timeout_seconds=3600, run_deadline_seconds=60), started_at=START
    )
    assert budget.tool_deadline(now=START) == START + timedelta(seconds=60)
    budget = LoopBudget(
        RunLimits(tool_timeout_seconds=30, run_deadline_seconds=3600), started_at=START
    )
    assert budget.tool_deadline(now=START) == START + timedelta(seconds=30)


def test_retry_and_reconcile_budgets_are_exposed() -> None:
    budget = LoopBudget(RunLimits(backend_retries=2, reconcile_budget=5))
    assert budget.backend_retries == 2
    assert budget.reconcile_budget == 5


def test_initial_used_counts_are_respected() -> None:
    """恢复重入时沿用已消耗量:已用满的预算立即超限。"""
    budget = LoopBudget(RunLimits(max_model_rounds=1), model_rounds_used=1)
    with pytest.raises(LoopLimitExceeded) as exc:
        budget.record_model_round()
    assert exc.value.failure_code == "LOOP_LIMIT_EXCEEDED"


def test_from_definition_overrides_conservative_defaults() -> None:
    limits = RunLimits.from_definition(
        {"max_model_rounds": 3, "max_tool_calls": 7, "unknown_key": "ignored"}
    )
    assert limits.max_model_rounds == 3
    assert limits.max_tool_calls == 7
    # 未覆盖字段保持保守默认。
    assert limits.tool_timeout_seconds == RunLimits().tool_timeout_seconds


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_model_rounds": 0},
        {"max_model_rounds": -1},
        {"max_model_rounds": "not-a-number"},
        {"max_tool_calls": None},
    ],
)
def test_from_definition_rejects_invalid_values_back_to_defaults(
    overrides: dict[str, Any],
) -> None:
    """非法/过小的覆盖不生效:回落保守默认,fail closed。"""
    limits = RunLimits.from_definition(overrides)
    assert limits.max_model_rounds == RunLimits().max_model_rounds
    assert limits.max_tool_calls == RunLimits().max_tool_calls
