"""Runtime 确定性 Eval 套件测试(计划 05 Task 4)。

- 计划原文用例:runtime_suite.case_ids 必须覆盖十个 Runtime 不变式;
- 数据集声明(eval/datasets/runtime/*.yaml)与执行代码按 id 分派一一对应;
- run_all() 对每个用例产出确定性结果且全部通过(failed/infrastructure_errors/
  skipped 均为 0),结果携带 case_id/category/passed/included_in_quality_score。

所有用例走真实 Runtime 服务与真库(复用根 conftest 的已迁移测试库)。
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eval.runtime_suite import (
    REQUIRED_CASE_IDS,
    RUNTIME_DATASET_DIR,
    RuntimeSuite,
    SuiteReport,
    load_runtime_dataset,
)
from eval.schema import SCHEMA_VERSION


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def runtime_suite(engine: AsyncEngine) -> RuntimeSuite:
    """从仓库内数据集目录加载的 Runtime 套件(绑定已迁移测试库引擎)。"""
    return RuntimeSuite(engine, dataset=RUNTIME_DATASET_DIR)


# ---------------------------------------------------------------------------
# 计划原文用例
# ---------------------------------------------------------------------------


def test_runtime_suite_contains_required_invariants(runtime_suite: RuntimeSuite) -> None:
    assert runtime_suite.case_ids >= {
        "tool-delta-never-executes",
        "input-resume-revalidates",
        "confirmation-toctou",
        "write-result-after-cancel",
        "unknown-reconciles-first",
        "cancelled-never-resumes",
        "event-replay-deduplicates",
        "context-summary-preserves-protected-facts",
        "posttool-invalid-output-keeps-write-outcome",
        "deleted-memory-never-reenters-context",
    }


# ---------------------------------------------------------------------------
# 数据集声明:数据声明期望,代码执行验证
# ---------------------------------------------------------------------------


def test_required_case_ids_are_covered_by_required_constant() -> None:
    assert REQUIRED_CASE_IDS >= {
        "tool-delta-never-executes",
        "input-resume-revalidates",
        "confirmation-toctou",
        "write-result-after-cancel",
        "unknown-reconciles-first",
        "cancelled-never-resumes",
        "event-replay-deduplicates",
        "context-summary-preserves-protected-facts",
        "posttool-invalid-output-keeps-write-outcome",
        "deleted-memory-never-reenters-context",
    }


def test_dataset_directory_declares_all_required_cases() -> None:
    """五个数据集文件共声明十个必需用例,均带 category/risk_level/期望摘要。"""
    cases = load_runtime_dataset(RUNTIME_DATASET_DIR)
    by_id = {case.id: case for case in cases}
    assert set(by_id) == REQUIRED_CASE_IDS
    for case_id, case in by_id.items():
        assert case.category, case_id
        assert case.risk_level in ("low", "medium", "high")
        assert case.expectation.strip(), case_id


def test_suite_case_ids_come_from_dataset(runtime_suite: RuntimeSuite) -> None:
    declared = {case.id for case in load_runtime_dataset(RUNTIME_DATASET_DIR)}
    assert runtime_suite.case_ids == declared


def test_load_rejects_unknown_case_id(tmp_path: Path) -> None:
    """数据集声明了没有执行函数的用例 id 时拒绝加载(数据与代码漂移)。"""
    (tmp_path / "core.yaml").write_text(
        f"schema_version: {SCHEMA_VERSION}\n"
        "cases:\n"
        "  - id: not-implemented-anywhere\n"
        "    category: backend_streaming\n"
        "    risk_level: low\n"
        "    expectation: 无执行函数的声明必须被拒绝\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not-implemented-anywhere"):
        load_runtime_dataset(tmp_path)


# ---------------------------------------------------------------------------
# run_all:确定性结果,全部通过
# ---------------------------------------------------------------------------


async def test_run_all_passes_all_cases_deterministically(
    runtime_suite: RuntimeSuite,
) -> None:
    first = await runtime_suite.run_all()
    assert (first.failed, first.infrastructure_errors, first.skipped) == (0, 0, 0)
    assert first.passed == len(REQUIRED_CASE_IDS)
    assert len(first.results) == len(REQUIRED_CASE_IDS)

    # 确定性:再次运行,用例集合与逐例通过结论完全一致(独立种子+清理)。
    second = await runtime_suite.run_all()
    assert [result.case_id for result in second.results] == [
        result.case_id for result in first.results
    ]
    assert [result.passed for result in second.results] == [
        result.passed for result in first.results
    ]
    assert second.summary_line() == first.summary_line()


async def test_results_carry_decision_fields(runtime_suite: RuntimeSuite) -> None:
    report = await runtime_suite.run_all()
    for result in report.results:
        assert result.case_id in REQUIRED_CASE_IDS
        assert result.category
        assert result.passed is True
        assert result.included_in_quality_score is True
        assert result.status == "PASSED"
        assert result.error is None


def test_summary_line_reports_required_counts() -> None:
    assert SuiteReport(results=()).summary_line() == (
        "failed=0 infrastructure_errors=0 skipped=0"
    )


def test_suite_is_constructible_from_repo_dataset_path(engine: AsyncEngine) -> None:
    suite = RuntimeSuite(engine, dataset=_PROJECT_ROOT / "eval" / "datasets" / "runtime")
    assert suite.case_ids == REQUIRED_CASE_IDS
