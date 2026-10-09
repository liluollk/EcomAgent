"""Held-out 任务完成率发布门禁测试。

夹具使用合成 CaseResult/ScoredCaseResult，经真实 ReportBuilder 组装报告；
不发网络请求、不碰数据库。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eval.gates.check_release import main as check_release_main
from eval.gates.derive_baseline import BaselineDerivationError, derive_baseline
from eval.gates.derive_baseline import main as derive_baseline_main
from eval.gates.schema import Baselines, GateResult, HardRules, ReleaseGate
from eval.reporting.json_report import ReportBuilder
from eval.results import CaseResult, FinalState
from eval.scorers.composite_result import ScoredCaseResult
from eval.scorers.decision import DecisionScore
from eval.scorers.grounding import GroundingScore
from eval.scorers.process import ProcessScore
from eval.scorers.recovery import RecoveryScore
from eval.scorers.safety import SafetyScore
from eval.scorers.task_completion import TaskCompletionScore


FINGERPRINT: str = "fp-gate-001"


# ---- 合成输入(经真实 ReportBuilder 组装报告) --------------------------


def _case_result(
    case_id: str,
    *,
    status: str = "COMPLETED",
    included: bool = True,
    error: str | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        repetition=0,
        status=status,  # type: ignore[arg-type]
        included_in_quality_score=included,
        fingerprint=FINGERPRINT,
        run_id=None,
        error=error,
        events=(),
        tool_calls=(),
        final_state=(
            FinalState(listings=(), logical_writes=1) if status == "COMPLETED" else None
        ),
    )


def _scored(
    case_id: str,
    *,
    task_passed: bool = True,
    false_rejections: int = 0,
    dangerous: int = 0,
    duplicate_writes: int = 0,
) -> ScoredCaseResult:
    return ScoredCaseResult(
        case_id=case_id,
        repetition=0,
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
            decision_accuracy=(false_rejections == 0),
            correct_decisions=1 if false_rejections == 0 else 0,
            total_decisions=1,
            legal_write_decisions=1,
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
            passed=(dangerous == 0),
            applicable=True,
            dangerous_attempts=dangerous,
            prevented_attempts=0,
            actual_dangerous_side_effects=dangerous,
        ),
        recovery=RecoveryScore(
            recovery_applicable=True,
            recovery_success=True,
            unknown_encounters=0,
            unknown_handled_correctly=0,
            duplicate_logical_writes=duplicate_writes,
            paths_match_allowed=True,
        ),
    )


def clean_results() -> list[CaseResult | ScoredCaseResult]:
    """两次计入质量分的执行:一过一未完成 → 完成率 0.5、误拒率 0.0,无写安全缺陷。"""
    return [
        _case_result("case-good"),
        _scored("case-good"),
        _case_result("case-mid"),
        _scored("case-mid", task_passed=False),
    ]


def build_report(
    results: list[CaseResult | ScoredCaseResult],
    *,
    fingerprint: str = FINGERPRINT,
    split: str = "held_out",
) -> dict[str, Any]:
    """经真实 ReportBuilder 组装报告(真实分母、协议指纹、脱敏结构)。"""
    report = ReportBuilder().build(results, protocol_fingerprint=fingerprint)
    report["dataset"] = {"split": split}
    return report


def report_with_completion(passed: int, total: int) -> dict[str, Any]:
    """构造完成率 ``passed/total`` 的报告(各例其余维度全过)。"""
    results: list[CaseResult | ScoredCaseResult] = []
    for index in range(total):
        case_id = f"case-{index:03d}"
        results.append(_case_result(case_id))
        results.append(_scored(case_id, task_passed=index < passed))
    return build_report(results)


def report_with_protocol(fingerprint: str) -> dict[str, Any]:
    """计划用例助手:同一份干净报告,仅协议指纹不同。"""
    return build_report(clean_results(), fingerprint=fingerprint)


def with_attestations(
    report: dict[str, Any], *, runtime: bool, e2e: bool
) -> dict[str, Any]:
    """附上 Runtime 确定性套件与 E2E 的通过声明字段。"""
    return {**report, "runtime_suite_passed": runtime, "e2e_passed": e2e}


@pytest.fixture()
def gate() -> ReleaseGate:
    """从干净合格报告派生的 Gate(基线:完成率 0.5、误拒率 0.0)。"""
    return derive_baseline(build_report(clean_results()))


# ---- 发布门禁 -----------------------------------------------------------


def test_release_gate_rejects_protocol_drift(gate: ReleaseGate) -> None:
    """计划原文用例:协议指纹与 Gate 记录不一致即拒绝。"""
    result = gate.check(report_with_protocol("different"))
    assert result.passed is False
    assert "PROTOCOL_MISMATCH" in result.failures


# ---- 补充语义:硬门槛 ---------------------------------------------------


def test_clean_report_passes_all_hard_rules(gate: ReleaseGate) -> None:
    """与基线同分布的干净报告(未声明 runtime/E2E)全部通过。"""
    result = gate.check(build_report(clean_results()))
    assert result.passed is True
    assert result.failures == []


def test_release_gate_only_compares_held_out_task_completion(
    gate: ReleaseGate,
) -> None:
    """Gate 的质量回归维度只有 held-out 任务完成率。"""
    assert set(Baselines.model_fields) == {"task_completion_rate"}
    assert set(HardRules.model_fields) == {"task_completion_tolerance"}
    result = gate.check(build_report(clean_results(), split="development"))
    assert result.passed is False
    assert result.failures == ["DATASET_SPLIT_MISMATCH"]


def test_infrastructure_error_fails_gate(gate: ReleaseGate) -> None:
    """报告含基础设施错误即失败(该例不入分母,其余指标不变)。"""
    report = build_report(
        [
            *clean_results(),
            _case_result(
                "case-infra",
                status="INFRASTRUCTURE_ERROR",
                included=False,
                error="provider 401",
            ),
        ]
    )
    result = gate.check(report)
    assert result.passed is False
    assert result.failures == ["INFRASTRUCTURE_ERROR"]


def test_invalid_protocol_fails_gate(gate: ReleaseGate) -> None:
    """有 SKIPPED 等情况导致协议无效时，不允许比较任务完成率。"""
    report = build_report(clean_results())
    report["totals"]["protocol_valid"] = False
    result = gate.check(report)
    assert result.passed is False
    assert result.failures == ["INVALID_PROTOCOL"]


def test_empty_denominator_fails_gate(gate: ReleaseGate) -> None:
    """真实分母为 0(无可信结果)即失败。"""
    report = build_report([])
    assert report["totals"]["quality_denominator"] == 0
    result = gate.check(report)
    assert result.passed is False
    assert result.failures == ["EMPTY_DENOMINATOR"]


def test_runtime_suite_failure_fails_gate(gate: ReleaseGate) -> None:
    """报告声明 Runtime 确定性套件未全过 → RUNTIME_SUITE_NOT_PASSED。"""
    report = with_attestations(build_report(clean_results()), runtime=False, e2e=True)
    result = gate.check(report)
    assert result.passed is False
    assert "RUNTIME_SUITE_NOT_PASSED" in result.failures


def test_e2e_failure_fails_gate(gate: ReleaseGate) -> None:
    """报告声明 E2E 未全过 → E2E_NOT_PASSED。"""
    report = with_attestations(build_report(clean_results()), runtime=True, e2e=False)
    result = gate.check(report)
    assert result.passed is False
    assert "E2E_NOT_PASSED" in result.failures


def test_attested_runtime_and_e2e_pass(gate: ReleaseGate) -> None:
    """声明全部通过时记录为 passed,不产生失败。"""
    report = with_attestations(build_report(clean_results()), runtime=True, e2e=True)
    result = gate.check(report)
    assert result.passed is True
    assert result.details["attestations"] == {
        "runtime_suite_passed": "passed",
        "e2e_passed": "passed",
    }


def test_unattested_runtime_and_e2e_are_noted_not_failed(gate: ReleaseGate) -> None:
    """报告未声明 runtime/E2E 结果时 Gate 不虚构:记 not_attested,不判失败。"""
    result = gate.check(build_report(clean_results()))
    assert result.passed is True
    assert result.details["attestations"] == {
        "runtime_suite_passed": "not_attested",
        "e2e_passed": "not_attested",
    }


# ---- 补充语义:容差门槛 -------------------------------------------------


def test_task_completion_regression_fails_gate(gate: ReleaseGate) -> None:
    """完成率 0.0 < 基线 0.5 − 2pp → TASK_COMPLETION_REGRESSION。"""
    result = gate.check(report_with_completion(0, 2))
    assert result.passed is False
    assert "TASK_COMPLETION_REGRESSION" in result.failures


def test_task_completion_within_tolerance_passes(gate: ReleaseGate) -> None:
    """完成率 0.49 仍高于下限 0.48(基线 0.5 − 2pp)→ 不判回归。"""
    result = gate.check(report_with_completion(49, 100))
    assert result.passed is True
    assert "TASK_COMPLETION_REGRESSION" not in result.failures


def test_gate_result_structure(gate: ReleaseGate) -> None:
    """GateResult 结构:passed/failures/details/checked_at 齐备。"""
    result = gate.check(build_report(clean_results()))
    assert isinstance(result, GateResult)
    assert isinstance(result.passed, bool)
    assert isinstance(result.failures, list)
    assert isinstance(result.details, dict)
    assert result.checked_at.tzinfo is not None


# ---- 补充语义:基线派生资格 ---------------------------------------------


def test_derive_baseline_captures_fingerprint_and_baselines() -> None:
    """派生结果记录协议指纹、完成率基线、固定容差与来源报告摘要。"""
    gate = derive_baseline(build_report(clean_results()))
    assert gate.gate_version == "v1"
    assert gate.protocol_fingerprint == FINGERPRINT
    assert gate.baselines.task_completion_rate == pytest.approx(0.5)
    assert gate.hard_rules.task_completion_tolerance == 0.02
    assert gate.derived_from_report.totals["quality_denominator"] == 2
    assert gate.created_at.tzinfo is not None


def test_release_gate_is_frozen() -> None:
    """Gate 一经派生即冻结:任何字段不可再赋值。"""
    gate = derive_baseline(build_report(clean_results()))
    with pytest.raises(ValidationError):
        gate.gate_version = "v2"


def test_derive_baseline_rejects_invalid_protocol() -> None:
    """protocol_valid 为 False(skipped 使整轮协议失效)→ 拒绝派生。"""
    report = build_report(clean_results())
    report["totals"]["protocol_valid"] = False
    with pytest.raises(BaselineDerivationError, match="protocol_valid"):
        derive_baseline(report)


def test_derive_baseline_rejects_infrastructure_errors() -> None:
    """含基础设施错误的报告不得成为基线。"""
    report = build_report(
        [
            *clean_results(),
            _case_result(
                "case-infra",
                status="INFRASTRUCTURE_ERROR",
                included=False,
                error="provider 401",
            ),
        ]
    )
    with pytest.raises(BaselineDerivationError, match="infrastructure"):
        derive_baseline(report)


def test_baseline_requires_held_out_report() -> None:
    """Development 报告不能派生能力基线。"""
    with pytest.raises(BaselineDerivationError, match="held_out"):
        derive_baseline(build_report(clean_results(), split="development"))


def test_derive_baseline_rejects_empty_denominator() -> None:
    """真实分母为 0 的报告不得成为基线。"""
    with pytest.raises(BaselineDerivationError, match="denominator"):
        derive_baseline(build_report([]))


def test_derive_baseline_rejects_missing_fingerprint() -> None:
    """空协议指纹的门无比较意义 → 拒绝派生。"""
    with pytest.raises(BaselineDerivationError, match="protocol_fingerprint"):
        derive_baseline(build_report(clean_results(), fingerprint=""))


# ---- CLI:派生与审查退出码 ----------------------------------------------


def _write_gate(tmp_path: Path) -> Path:
    gate = derive_baseline(build_report(clean_results()))
    gate_path = tmp_path / "v1.json"
    gate_path.write_text(gate.model_dump_json(), encoding="utf-8")
    return gate_path


def _write_report(tmp_path: Path, report: dict[str, Any]) -> Path:
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path


def test_derive_cli_writes_gate_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 从报告 JSON 派生 Gate 并写出可回读的文件。"""
    report_path = _write_report(tmp_path, build_report(clean_results()))
    gate_path = tmp_path / "v1.json"

    code = derive_baseline_main(
        ["--report", str(report_path), "--output", str(gate_path)]
    )

    assert code == 0
    gate = ReleaseGate.model_validate_json(gate_path.read_text(encoding="utf-8"))
    assert gate.protocol_fingerprint == FINGERPRINT
    assert gate.baselines.task_completion_rate == pytest.approx(0.5)
    assert FINGERPRINT in capsys.readouterr().out


def test_derive_cli_refuses_ineligible_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """不合格报告 → 退出码 2、不产出 Gate 文件、错误说明原因。"""
    report_path = _write_report(
        tmp_path, build_report(clean_results(), split="development")
    )
    gate_path = tmp_path / "v1.json"

    code = derive_baseline_main(
        ["--report", str(report_path), "--output", str(gate_path)]
    )

    assert code == 2
    assert not gate_path.exists()
    assert "held_out" in capsys.readouterr().err


def test_derive_cli_refuses_overwrite(tmp_path: Path) -> None:
    """Gate 只能派生一次:已存在的输出文件即拒绝(退出码 2)。"""
    report_path = _write_report(tmp_path, build_report(clean_results()))
    gate_path = tmp_path / "v1.json"
    first = derive_baseline_main(
        ["--report", str(report_path), "--output", str(gate_path)]
    )
    assert first == 0

    second = derive_baseline_main(
        ["--report", str(report_path), "--output", str(gate_path)]
    )

    assert second == 2


def test_check_cli_passes_clean_report(tmp_path: Path) -> None:
    """审查通过 → 退出码 0。"""
    gate_path = _write_gate(tmp_path)
    report_path = _write_report(tmp_path, build_report(clean_results()))
    code = check_release_main(["--gate", str(gate_path), "--report", str(report_path)])
    assert code == 0


def test_check_cli_rejects_non_held_out_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """审查失败 → 退出码 1,失败码出现在输出中。"""
    gate_path = _write_gate(tmp_path)
    report_path = _write_report(
        tmp_path, build_report(clean_results(), split="development")
    )
    code = check_release_main(["--gate", str(gate_path), "--report", str(report_path)])
    assert code == 1
    assert "DATASET_SPLIT_MISMATCH" in capsys.readouterr().out


def test_check_cli_missing_gate_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gate 文件不可读 → 前置错误退出码 2。"""
    report_path = _write_report(tmp_path, build_report(clean_results()))
    code = check_release_main(
        ["--gate", str(tmp_path / "missing.json"), "--report", str(report_path)]
    )
    assert code == 2
    assert "missing.json" in capsys.readouterr().err
