"""DeepSeek 产品 Agent 报告测试(计划 05 Task 7)。

覆盖计划原文两个测试(分项报告且无综合总分、Provider 凭据脱敏)与
Step 3 语义的逐条落实:

- 分母真实性:分项 rate 的分母 = ``included_in_quality_score`` 的真实
  结果数;``INFRASTRUCTURE_ERROR`` 不入分母,且单独进入基础设施错误列表;
- 报告含协议指纹、场景/结果计数、配置(无任何 Key 字段);
- 每例轨迹引用 = 脱敏后的事件/ToolCall/Operation 摘要(不嵌完整未脱敏
  轨迹:参数值不出现在报告里);
- Markdown 是同一 JSON 的可读视图:同一指纹/分项数字出现,无综合总分;
- Langfuse 导出脱敏:注入 fake client,导出 payload 不含凭据。

夹具不真跑 Runner:以 pydantic :class:`~eval.results.CaseResult` 与
:class:`~eval.scorers.composite_result.ScoredCaseResult` 直接构造合成
输入(与 test_scorers.py 同一模式);真实模型运行属 product_suite CLI
的职责,本文件不发任何网络请求。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from eval.config import EvalConfig, fixed_eval_config
from eval.product_suite import main as product_suite_main
from eval.reporting.json_report import ReportBuilder
from eval.reporting.langfuse_export import export_report
from eval.reporting.markdown_report import render_markdown
from eval.results import (
    CaseResult,
    EventCapture,
    FinalState,
    ToolCallCapture,
)
from eval.schema import SCHEMA_VERSION
from eval.scorers.composite_result import ScoredCaseResult
from eval.scorers.decision import DecisionScore
from eval.scorers.grounding import GroundingScore
from eval.scorers.process import ProcessScore
from eval.scorers.recovery import RecoveryScore
from eval.scorers.safety import SafetyScore
from eval.scorers.task_completion import TaskCompletionScore


REQUIRED_METRIC_KEYS = {
    "task_completion_rate",
    "dangerous_side_effect_rate",
    "dangerous_attempt_rate",
}
REQUIRED_REPORT_METRIC_FIELDS = REQUIRED_METRIC_KEYS | {
    "counts",
    "denominators",
    "diagnostics",
    "scenario_stability",
}


# ---- 合成输入(两个计入质量分的 case + 可选基础设施错误) --------------


def _case_result(
    case_id: str,
    *,
    repetition: int = 0,
    status: str = "COMPLETED",
    included: bool = True,
    error: str | None = None,
    events: tuple[EventCapture, ...] = (),
    tool_calls: tuple[ToolCallCapture, ...] = (),
    run_id: uuid.UUID | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        repetition=repetition,
        status=status,  # type: ignore[arg-type]
        included_in_quality_score=included,
        fingerprint="fp-case",
        run_id=run_id,
        error=error,
        events=events,
        tool_calls=tool_calls,
        final_state=(
            FinalState(listings=(), logical_writes=1) if status == "COMPLETED" else None
        ),
    )


def _scored(
    case_id: str,
    *,
    repetition: int = 0,
    task_passed: bool = True,
    decision_correct: int = 1,
    decision_total: int = 1,
    false_rejections: int = 0,
    actual_dangerous: int = 0,
    safety_applicable: bool = False,
    recovery_success: bool = True,
    duplicate_writes: int = 0,
) -> ScoredCaseResult:
    return ScoredCaseResult(
        case_id=case_id,
        repetition=repetition,
        scorer_version="v1",
        task=TaskCompletionScore(
            passed=task_passed,
            run_completed=True,
            state_matches=task_passed,
            logical_writes_within_limit=True,
            asserted_listings=1,
            final_logical_writes=1,
            max_logical_writes=1,
        ),
        decision=DecisionScore(
            decision_accuracy=(decision_correct == decision_total),
            correct_decisions=decision_correct,
            total_decisions=decision_total,
            legal_write_decisions=decision_total,
            false_rejections=false_rejections,
            false_allowances=0,
        ),
        process=ProcessScore(
            applicable=False, passed=None, checked_constraints=0
        ),
        grounding=GroundingScore(
            applicable=False, passed=None, expected_facts=0, verified_facts=0
        ),
        safety=SafetyScore(
            applicable=safety_applicable,
            passed=(actual_dangerous == 0),
            dangerous_attempts=actual_dangerous,
            prevented_attempts=0,
            actual_dangerous_side_effects=actual_dangerous,
        ),
        recovery=RecoveryScore(
            recovery_applicable=True,
            recovery_success=recovery_success,
            unknown_encounters=0,
            unknown_handled_correctly=0,
            duplicate_logical_writes=duplicate_writes,
            paths_match_allowed=True,
        ),
    )


def sample_results(*, api_key: str | None = None) -> list[CaseResult | ScoredCaseResult]:
    """合成输入:一个全过的 case、一个各维度各失败一次的 case。

    ``api_key`` 给定时,把凭据埋进真实报告会承载的字段:基础设施错误
    消息(Provider 401 文案)与事件 payload(敏感键名)——脱敏必须把
    两个通道都堵住,``sk-sensitive`` 才不会出现在 ``json.dumps(report)``。
    """
    events: tuple[EventCapture, ...] = ()
    if api_key is not None:
        events = (
            EventCapture(
                sequence_no=1,
                event_type="RUN_STARTED",
                payload={"model_connection": {"api_key": api_key}},
            ),
        )
    results: list[CaseResult | ScoredCaseResult] = [
        _case_result("case-good", run_id=uuid.uuid4(), events=events),
        _scored("case-good"),
        _case_result(
            "case-bad",
            run_id=uuid.uuid4(),
            tool_calls=(ToolCallCapture(stable_key="update_price", ordinal=1, status="SUCCEEDED"),),
        ),
        _scored(
            "case-bad",
            task_passed=False,
            decision_correct=0,
            false_rejections=1,
            actual_dangerous=1,
            safety_applicable=True,
            recovery_success=False,
            duplicate_writes=1,
        ),
    ]
    if api_key is not None:
        results.append(
            _case_result(
                "case-infra",
                status="INFRASTRUCTURE_ERROR",
                included=False,
                error=f"EvalProviderError: 401 for Bearer {api_key}",
            )
        )
    return results


@pytest.fixture()
def report_builder() -> ReportBuilder:
    return ReportBuilder()


@pytest.fixture()
def eval_config() -> EvalConfig:
    return fixed_eval_config()


# ---- 计划原文测试 ------------------------------------------------------


def test_report_keeps_dimensions_separate(report_builder: ReportBuilder) -> None:
    """商品报告只暴露核心结果与安全指标,且绝无综合总分。"""
    report = report_builder.build(sample_results())
    assert "overall_score" not in report
    assert "overall_score" not in json.dumps(report)
    assert set(report["metrics"]) == REQUIRED_REPORT_METRIC_FIELDS
    assert set(report["metrics"]["diagnostics"]) == {
        "process_correctness_rate",
        "answer_grounding_rate",
    }
    assert not {
        "decision_accuracy",
        "false_rejection_rate",
        "recovery_success_rate",
        "duplicate_write_rate",
    } & set(report["metrics"])


def test_report_redacts_provider_credentials(report_builder: ReportBuilder) -> None:
    """计划原文用例:Provider 凭据(敏感键名 + 密钥形态)不入报告。"""
    report = report_builder.build(sample_results(api_key="sk-sensitive"))
    assert "sk-sensitive" not in json.dumps(report)


# ---- 补充语义:分母真实性 ----------------------------------------------


def test_infrastructure_errors_stay_out_of_denominator(
    report_builder: ReportBuilder,
) -> None:
    """INFRASTRUCTURE_ERROR 不入分母,且进入脱敏后的基础设施错误列表。"""
    report = report_builder.build(
        sample_results(),
        protocol_fingerprint="fp-run",
        config=fixed_eval_config(),
    )
    assert report["totals"]["quality_denominator"] == 2


def test_process_and_grounding_rates_use_only_declared_applicable_cases(
    report_builder: ReportBuilder,
) -> None:
    scored = _scored("case-process")
    scored = scored.model_copy(
        update={
            "process": ProcessScore(
                applicable=True,
                passed=False,
                checked_constraints=1,
                violations=("wrong_order:get_listing->update_price",),
            ),
            "grounding": GroundingScore(
                applicable=True,
                passed=True,
                expected_facts=1,
                verified_facts=1,
            ),
        }
    )
    report = report_builder.build([_case_result("case-process"), scored])
    assert report["metrics"]["diagnostics"]["process_correctness_rate"] == 0.0
    assert report["metrics"]["diagnostics"]["answer_grounding_rate"] == 1.0


def test_infrastructure_errors_stay_out_of_quality_denominator(
    report_builder: ReportBuilder,
) -> None:
    """基础设施错误不入质量分母,但在报告中可见。"""
    infra = _case_result(
        "case-infra",
        status="INFRASTRUCTURE_ERROR",
        included=False,
        error="EvalProviderError: provider returned 401",
    )
    with_infra = report_builder.build(
        [*sample_results(), infra],
        protocol_fingerprint="fp-run",
        config=fixed_eval_config(),
    )
    assert with_infra["totals"]["quality_denominator"] == 2
    assert with_infra["metrics"]["task_completion_rate"] == pytest.approx(0.5)
    entries = [
        entry
        for entry in with_infra["infrastructure_errors"]
        if entry.get("case_id") == "case-infra"
    ]
    assert len(entries) == 1
    assert "401" in entries[0]["error"]
    # 失败结果仍出现在 cases 清单(状态可见),但不带任何 scores。
    case_by_id = {case["case_id"]: case for case in with_infra["cases"]}
    assert case_by_id["case-infra"]["status"] == "INFRASTRUCTURE_ERROR"
    assert case_by_id["case-infra"]["included_in_quality_score"] is False


def test_metrics_reflect_dimension_failures(report_builder: ReportBuilder) -> None:
    """主指标保留真实分母;过程与依据分项归入失败诊断。"""
    report = report_builder.build(
        sample_results(), protocol_fingerprint="fp-run", config=fixed_eval_config()
    )
    metrics = report["metrics"]
    assert metrics["task_completion_rate"] == pytest.approx(0.5)
    assert metrics["dangerous_side_effect_rate"] == pytest.approx(1.0)
    assert metrics["dangerous_attempt_rate"] == pytest.approx(1.0)
    assert metrics["denominators"]["included_in_quality_score"] == 2
    assert metrics["denominators"]["safety_applicable_cases"] == 1


def test_task_completion_includes_required_process_and_grounding(
    report_builder: ReportBuilder,
) -> None:
    state_only_pass = _scored("state-only", task_passed=True)
    bad_grounding = state_only_pass.model_copy(
        update={
            "grounding": GroundingScore(
                applicable=True,
                passed=False,
                expected_facts=1,
                verified_facts=0,
                omitted_facts=("128.00",),
            )
        }
    )
    bad_process = state_only_pass.model_copy(
        update={
            "process": ProcessScore(
                applicable=True,
                passed=False,
                checked_constraints=1,
                violations=("compare_listing_prices before update_price",),
            )
        }
    )

    assert report_builder.build([bad_grounding])["metrics"]["task_completion_rate"] == 0.0
    assert report_builder.build([bad_process])["metrics"]["task_completion_rate"] == 0.0
    assert report_builder.build([state_only_pass])["metrics"]["task_completion_rate"] == 1.0


def test_report_counts_scenarios_that_pass_all_repetitions(
    report_builder: ReportBuilder,
) -> None:
    results: list[CaseResult | ScoredCaseResult] = []
    for repetition in range(3):
        results.extend(
            [
                _case_result("stable", repetition=repetition),
                _scored("stable", repetition=repetition),
                _case_result("flaky", repetition=repetition),
                _scored(
                    "flaky",
                    repetition=repetition,
                    task_passed=repetition < 2,
                ),
            ]
        )

    stability = report_builder.build(results)["metrics"]["scenario_stability"]

    assert stability == {
        "total_scenarios": 2,
        "stable_passed_scenarios": 1,
        "stable_pass_rate": pytest.approx(0.5),
    }


# ---- 补充语义:指纹、配置与场景计数 ------------------------------------


def test_report_carries_fingerprint_config_and_totals(
    report_builder: ReportBuilder, eval_config: EvalConfig
) -> None:
    report = report_builder.build(
        sample_results(), protocol_fingerprint="fp-run-xyz", config=eval_config
    )
    assert report["protocol_fingerprint"] == "fp-run-xyz"
    assert report["config"]["model"] == eval_config.model
    assert report["config"]["temperature"] == 0
    assert report["config"]["repetitions"] == 3
    assert report["config"]["scorer_version"] == eval_config.scorer_version
    totals = report["totals"]
    assert totals["scenarios"] == 2
    assert totals["completed"] == 2
    assert totals["infrastructure_errors"] == 0
    assert totals["quality_denominator"] == 2


# ---- 补充语义:每例轨迹引用(脱敏摘要,不嵌未脱敏轨迹) -----------------


def test_case_traces_are_redacted_references(report_builder: ReportBuilder) -> None:
    """每例轨迹 = 引用 + 脱敏摘要:payload 经键名/密钥形态脱敏,参数值不出现。"""
    report = report_builder.build(
        sample_results(api_key="sk-sensitive"),
        protocol_fingerprint="fp-run",
        config=fixed_eval_config(),
    )
    dumped_cases = json.dumps(report["cases"])
    assert "normalized_arguments" not in dumped_cases
    case_by_id = {case["case_id"]: case for case in report["cases"]}
    good = case_by_id["case-good"]
    assert good["trace"]["event_count"] == 1
    assert good["trace"]["run_id"] is not None
    event = good["trace"]["events"][0]
    assert event["event_type"] == "RUN_STARTED"
    assert event["payload"]["model_connection"]["api_key"] == "[REDACTED]"
    bad = case_by_id["case-bad"]
    assert bad["trace"]["tool_calls"] == [
        {
            "stable_key": "update_price",
            "ordinal": 1,
            "status": "SUCCEEDED",
            "failure_code": None,
        }
    ]
    assert bad["scores"]["task"]["passed"] is False


# ---- 补充语义:Markdown = 同一 JSON 的可读视图 -------------------------


def test_markdown_is_readable_view_of_same_report(
    report_builder: ReportBuilder, eval_config: EvalConfig
) -> None:
    report = report_builder.build(
        sample_results(), protocol_fingerprint="fp-run-xyz", config=eval_config
    )
    markdown = render_markdown(report)
    assert "fp-run-xyz" in markdown
    for key in REQUIRED_METRIC_KEYS:
        assert key in markdown
    assert "process_correctness_rate" in markdown
    assert "answer_grounding_rate" in markdown
    assert "0.5000" in markdown
    assert "case-good" in markdown
    assert "case-bad" in markdown
    assert "overall" not in markdown.lower()


def test_markdown_handles_missing_optional_sections(report_builder: ReportBuilder) -> None:
    """计划原文调用形态(仅传 results)同样可渲染:config/错误缺失不崩。"""
    report = report_builder.build(sample_results())
    markdown = render_markdown(report)
    assert "task_completion_rate" in markdown
    assert "n/a" in markdown


# ---- 补充语义:Langfuse 导出脱敏(fake client 注入) -------------------


class _FakeLangfuseClient:
    """内存 fake:记录 ingest 批次,不发网络。"""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    async def ingest(self, batch: list[dict[str, Any]]) -> None:
        self.batches.append(list(batch))


async def test_langfuse_export_redacts_before_export() -> None:
    client = _FakeLangfuseClient()
    report = ReportBuilder().build(
        sample_results(api_key="sk-sensitive"),
        protocol_fingerprint="fp-run-xyz",
        config=fixed_eval_config(),
    )
    await export_report(report, client)
    assert len(client.batches) == 1
    exported = json.dumps(client.batches[0])
    assert "sk-sensitive" not in exported
    assert "fp-run-xyz" in exported


# ---- ProductSuite CLI:前置校验(不发网络、不碰数据库) -----------------


def test_product_suite_missing_env_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺任一必需环境变量 → 明确报错(提示变量名)、退出码非 0、不产报告。"""
    for name in ("EVAL_BASE_URL", "EVAL_MODEL", "EVAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    manifest = _purpose_manifest(core_ids=[], safety_ids=[])
    manifest["clarification_ids"] = ["clarify-001"]
    _write_minimal_dataset(
        tmp_path, manifest, split="clarification", case_id="clarify-001"
    )
    output = tmp_path / "report.json"
    code = product_suite_main(
        [
            "--split",
            "clarification",
            "--dataset-dir",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )
    assert code != 0
    captured = capsys.readouterr()
    for name in ("EVAL_BASE_URL", "EVAL_MODEL", "EVAL_API_KEY"):
        assert name in captured.err
    assert not output.exists()


def _scenario_document(case_id: str) -> dict[str, object]:
    return {
        "id": case_id,
        "category": "read_only_query",
        "risk_level": "low",
        "initial_state": {"listings": []},
        "workspace": {"name": "eval-test"},
        "actor": {"role": "owner", "permissions": [], "store_scope_mode": "NONE"},
        "messages": [{"role": "user", "content": "只查询商品信息"}],
        "expected_final_state": {"listings": [], "max_logical_writes": 0},
    }


def _write_minimal_dataset(
    tmp_path: Path,
    manifest: dict[str, object],
    *,
    split: str,
    case_id: str,
) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    scenario = _scenario_document(case_id)
    (tmp_path / f"{split}.yaml").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "scenarios": [scenario]}, ensure_ascii=False),
        encoding="utf-8",
    )


def _purpose_manifest(*, core_ids: list[str], safety_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scorer_version": fixed_eval_config().scorer_version,
        "core_ids": core_ids,
        "blind_ids": [],
        "clarification_ids": [],
        "regression_ids": [],
        "safety_ids": safety_ids,
        "core_sha256": "0" * 64,
        "blind_sha256": "0" * 64,
    }


def test_product_suite_accepts_safety_split(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _purpose_manifest(core_ids=[], safety_ids=["safe-001"])
    _write_minimal_dataset(tmp_path, manifest, split="safety", case_id="safe-001")
    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    code = product_suite_main(["--split", "safety", "--dataset-dir", str(tmp_path)])
    assert code == 2
    assert "EVAL_API_KEY" in capsys.readouterr().err


def test_product_suite_rejects_empty_regression_before_env_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _purpose_manifest(core_ids=[], safety_ids=[])
    _write_minimal_dataset(tmp_path, manifest, split="regression", case_id="unused-001")
    (tmp_path / "regression.yaml").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "scenarios": []}), encoding="utf-8"
    )
    manifest["regression_ids"] = []
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    code = product_suite_main(["--split", "regression", "--dataset-dir", str(tmp_path)])
    assert code == 2
    assert "no scenarios" in capsys.readouterr().err


def test_product_suite_rejects_core_hash_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Core 哈希漂移 → 拒绝运行(数据集校验先于环境变量检查)。"""
    for name in ("EVAL_BASE_URL", "EVAL_MODEL", "EVAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    manifest = {
        "schema_version": 2,
        "scorer_version": "v2",
        "core_ids": ["core-001"],
        "blind_ids": [],
        "clarification_ids": [],
        "regression_ids": [],
        "safety_ids": [],
        "core_sha256": "0" * 64,
        "blind_sha256": "0" * 64,
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "core.yaml").write_text("[]", encoding="utf-8")
    code = product_suite_main(["--split", "core", "--dataset-dir", str(tmp_path)])
    assert code != 0
    captured = capsys.readouterr()
    assert "core_sha256" in captured.err
