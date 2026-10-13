"""故障恢复 Eval 套件测试(计划 05 Task 6)。

- 计划原文用例:``recovery_suite.result_for("write-applied-response-lost")``
  必须声明 ``UNKNOWN → APPLIED`` 的回查路径与恰好一次逻辑写;
- 数据集(eval/datasets/recovery/faults.yaml)声明 ≥7 类故障点(模型超时、
  Tool 调用前退出、写入已生效响应丢失、确认后 Worker 重启、平台暂时不可用、
  回查暂时不一致、重复提交),每例携带故障注名、期望回查路径、逻辑写上界
  与权威最终状态(价格 + Run 终态);
- ``run_all()`` 全部通过(failed/infrastructure_errors/skipped 均为 0)且恢复
  指标(recovery_success_rate、actual_duplicate_writes、
  final_state_consistency、unknown_handling_correct)达到发布门槛。

所有用例走真实 Runtime 服务、真实商品应用环境(MockCommerceAdapter +
FaultInjector)与真库(复用根 conftest 的已迁移测试库)。
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eval.recovery_suite import (
    RECOVERY_DATASET_PATH,
    REQUIRED_CASE_IDS,
    REQUIRED_FAULTS,
    RecoverySuite,
    RecoverySuiteReport,
    load_recovery_dataset,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FAULTS_YAML = _PROJECT_ROOT / "eval" / "datasets" / "recovery" / "faults.yaml"


@pytest.fixture()
def recovery_suite(engine: AsyncEngine) -> RecoverySuite:
    """从仓库内数据集加载的故障恢复套件(绑定已迁移测试库引擎)。"""
    return RecoverySuite(engine, dataset=_FAULTS_YAML)


# ---------------------------------------------------------------------------
# 计划原文用例
# ---------------------------------------------------------------------------


def test_recovery_suite_checks_authoritative_outcome(recovery_suite: RecoverySuite) -> None:
    result = recovery_suite.result_for("write-applied-response-lost")
    assert result.expected_reconcile_path == ["UNKNOWN", "APPLIED"]
    assert result.max_logical_writes == 1


# ---------------------------------------------------------------------------
# 必需故障点:数据集声明与执行代码一一对应
# ---------------------------------------------------------------------------


def test_recovery_suite_contains_required_fault_points(recovery_suite: RecoverySuite) -> None:
    assert recovery_suite.case_ids >= {
        "model-timeout",
        "worker-exit-before-write",
        "write-applied-response-lost",
        "worker-restart-after-confirmation",
        "platform-temporarily-unavailable",
        "reconcile-temporarily-inconsistent",
        "duplicate-submit",
    }
    assert len(recovery_suite.case_ids) >= 7


def test_required_constants_cover_plan_faults() -> None:
    assert REQUIRED_CASE_IDS >= {
        "model-timeout",
        "worker-exit-before-write",
        "write-applied-response-lost",
        "worker-restart-after-confirmation",
        "platform-temporarily-unavailable",
        "reconcile-temporarily-inconsistent",
        "duplicate-submit",
    }
    assert REQUIRED_FAULTS == {
        "model_timeout",
        "worker_exit_before_write",
        "response_lost_after_commit",
        "worker_restart_after_confirmation",
        "permanent_failure",
        "temporary_reconcile_unknown",
        "duplicate_submit",
    }


def test_dataset_declares_fault_annotations_and_expectations() -> None:
    """数据集每例声明故障注名、回查路径、写数上界与权威最终状态。"""
    cases = load_recovery_dataset(RECOVERY_DATASET_PATH)
    by_id = {case.id: case for case in cases}
    assert set(by_id) >= REQUIRED_CASE_IDS
    assert {case.fault for case in cases} >= REQUIRED_FAULTS
    assert len(cases) >= 7
    for case in cases:
        assert case.category, case.id
        assert case.risk_level in ("low", "medium", "high")
        assert case.expectation.strip(), case.id
        assert case.max_logical_writes >= 0, case.id
        assert case.expected_final_state.price, case.id
        assert case.expected_final_state.run_status, case.id
    declared = by_id["write-applied-response-lost"]
    assert list(declared.expected_reconcile_path) == ["UNKNOWN", "APPLIED"]
    assert declared.max_logical_writes == 1
    assert declared.expected_final_state.price == "99.00"
    assert declared.expected_final_state.run_status == "COMPLETED"


def test_result_for_unknown_case_raises(recovery_suite: RecoverySuite) -> None:
    with pytest.raises(KeyError):
        recovery_suite.result_for("no-such-fault-case")


# ---------------------------------------------------------------------------
# run_all:全部通过,恢复指标达到发布门槛,逐例断言权威最终状态与逻辑写数
# ---------------------------------------------------------------------------


async def test_run_all_passes_all_fault_cases(recovery_suite: RecoverySuite) -> None:
    first = await recovery_suite.run_all()
    failures = [
        f"{result.case_id}: {result.error}"
        for result in first.results
        if result.status != "PASSED"
    ]
    assert (first.failed, first.infrastructure_errors, first.skipped) == (0, 0, 0), failures
    assert first.passed == len(recovery_suite.case_ids)

    # 发布门槛指标:零重复写、成功率/最终状态一致率/UNKNOWN 处理正确率全满。
    assert first.actual_duplicate_writes == 0
    assert first.recovery_success_rate == 1.0
    assert first.final_state_consistency == 1.0
    assert first.unknown_handling_correct == 1.0
    assert first.summary_line() == (
        "actual_duplicate_writes=0 failed=0 infrastructure_errors=0 skipped=0"
    )

    # 逐例:最终权威状态与逻辑写数均按数据集声明断言。
    for result in first.results:
        assert result.status == "PASSED", f"{result.case_id}: {result.error}"
        assert result.included_in_quality_score is True
        assert result.error is None
        assert result.observed_run_status == result.expected_final_state.run_status
        assert result.observed_price == result.expected_final_state.price
        assert result.final_state_matches is True
        assert 0 <= result.observed_logical_writes <= result.max_logical_writes
        assert result.observed_reconcile_path == result.expected_reconcile_path
        assert result.path_matches is True

    # 确定性:再次运行,用例集合与逐例通过结论完全一致(独立种子 + 清理)。
    second = await recovery_suite.run_all()
    assert [result.case_id for result in second.results] == [
        result.case_id for result in first.results
    ]
    assert [result.passed for result in second.results] == [
        result.passed for result in first.results
    ]
    assert second.summary_line() == first.summary_line()


def test_summary_line_of_empty_report() -> None:
    assert RecoverySuiteReport(results=()).summary_line() == (
        "actual_duplicate_writes=0 failed=0 infrastructure_errors=0 skipped=0"
    )
