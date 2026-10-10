"""发布 Gate 审查(计划 05 Task 8 Step 3)。

:func:`check_release` 以派生好的 :class:`~eval.gates.schema.ReleaseGate`
审查一份报告,输出 :class:`~eval.gates.schema.GateResult`;失败码按下列
审查顺序追加,全部通过才 ``passed=True``。

硬门槛:

- ``PROTOCOL_MISMATCH``:报告评测基准指纹与 Gate 记录不一致(所有比较必须
  使用同一套题目、模型、采样配置和 Scorer);
- ``DATASET_SPLIT_MISMATCH``:只有 ``core`` 报告可用于能力基线比较;
- ``INVALID_PROTOCOL``:报告必须明确声明 ``totals.protocol_valid`` 为 True;
- ``INFRASTRUCTURE_ERROR``:报告含基础设施错误(计数 > 0 或错误列表非空);
- ``EMPTY_DENOMINATOR``:真实分母(``totals.quality_denominator``)缺失
  或为 0——没有可信结果就不存在"通过";
- ``RUNTIME_SUITE_NOT_PASSED`` / ``E2E_NOT_PASSED``:报告字段
  ``runtime_suite_passed`` / ``e2e_passed`` **明确为 False**。字段缺失或
  None 视为"未声明"(details 记 ``not_attested``):报告未声明时 Gate
  不虚构其结果,只审查报告实际承载的声明。

容差门槛(基线值或报告值缺失/为 None 时跳过,details 记 ``skipped``):

- ``TASK_COMPLETION_REGRESSION``:任务完成率 < 基线 − 2 个百分点。

CLI::

    python -m eval.gates.check_release --gate eval/gates/v1.json \
        --report reports/frozen-v1.json

退出码语义:通过 → 0;门槛失败 → 1;Gate/报告不可读或结构非法 → 2。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eval.gates.schema import GateResult, ReleaseGate, is_metric_number


FAIL_PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
FAIL_DATASET_SPLIT_MISMATCH = "DATASET_SPLIT_MISMATCH"
FAIL_INVALID_PROTOCOL = "INVALID_PROTOCOL"
FAIL_INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
FAIL_EMPTY_DENOMINATOR = "EMPTY_DENOMINATOR"
FAIL_RUNTIME_SUITE = "RUNTIME_SUITE_NOT_PASSED"
FAIL_E2E = "E2E_NOT_PASSED"
FAIL_TASK_COMPLETION_REGRESSION = "TASK_COMPLETION_REGRESSION"

ATTESTED_PASSED = "passed"
ATTESTED_FAILED = "failed"
ATTESTED_MISSING = "not_attested"


def check_release(gate: ReleaseGate, report: Mapping[str, Any]) -> GateResult:
    """以 Gate 审查报告;返回结构化结论(失败码按模块 docstring 顺序)。"""
    failures: list[str] = []
    totals = _mapping(report.get("totals"))
    metrics = _mapping(report.get("metrics"))
    details: dict[str, Any] = {"gate_version": gate.gate_version}

    # -- 硬门槛:协议指纹一致 --------------------------------------------
    actual_fingerprint = report.get("benchmark_fingerprint")
    details["benchmark_fingerprint"] = {
        "gate": gate.benchmark_fingerprint,
        "report": actual_fingerprint,
    }
    if actual_fingerprint != gate.benchmark_fingerprint:
        failures.append(FAIL_PROTOCOL_MISMATCH)

    dataset = report.get("dataset")
    split = dataset.get("split") if isinstance(dataset, dict) else None
    details["dataset_split"] = split
    if split != "core":
        failures.append(FAIL_DATASET_SPLIT_MISMATCH)

    if totals.get("protocol_valid") is not True:
        failures.append(FAIL_INVALID_PROTOCOL)

    # -- 硬门槛:无基础设施错误 ------------------------------------------
    infra_count = totals.get("infrastructure_errors")
    infra_entries = report.get("infrastructure_errors")
    details["infrastructure_errors"] = {
        "count": infra_count,
        "entries": len(infra_entries) if isinstance(infra_entries, list) else 0,
    }
    if (is_metric_number(infra_count) and infra_count > 0) or (
        isinstance(infra_entries, list) and len(infra_entries) > 0
    ):
        failures.append(FAIL_INFRASTRUCTURE_ERROR)

    # -- 硬门槛:真实分母非零 --------------------------------------------
    denominator = totals.get("quality_denominator")
    details["quality_denominator"] = denominator
    if not is_metric_number(denominator) or denominator <= 0:
        failures.append(FAIL_EMPTY_DENOMINATOR)

    # -- 硬门槛:Runtime 确定性套件与 E2E 全过(明确 False 即失败)-------
    runtime_attested = report.get("runtime_suite_passed")
    e2e_attested = report.get("e2e_passed")
    details["attestations"] = {
        "runtime_suite_passed": _attestation(runtime_attested),
        "e2e_passed": _attestation(e2e_attested),
    }
    if runtime_attested is False:
        failures.append(FAIL_RUNTIME_SUITE)
    if e2e_attested is False:
        failures.append(FAIL_E2E)

    # -- 容差门槛:任务完成率下限 -----------------------------------------
    task_check = _tolerance(
        baseline=gate.baselines.task_completion_rate,
        report_value=metrics.get("task_completion_rate"),
        tolerance=gate.hard_rules.task_completion_tolerance,
        bound="floor",
    )
    details["tolerances"] = {
        "task_completion_rate": task_check,
    }
    if task_check["status"] == "regression":
        failures.append(FAIL_TASK_COMPLETION_REGRESSION)

    return GateResult(passed=not failures, failures=failures, details=details)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI:``--gate X --report Y`` → 审查报告;通过 0 / 失败 1 / 前置错误 2。"""
    parser = argparse.ArgumentParser(
        prog="eval.gates.check_release",
        description="Check an eval report against a frozen release gate.",
    )
    parser.add_argument("--gate", required=True, help="path of the release gate JSON")
    parser.add_argument(
        "--report", required=True, help="path of the JSON report to check"
    )
    args = parser.parse_args(argv)

    try:
        gate = ReleaseGate.model_validate_json(
            Path(args.gate).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        print(f"check_release: cannot load gate {args.gate!r}: {error}", file=sys.stderr)
        return 2
    try:
        document: Any = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"check_release: cannot read report {args.report!r}: {error}",
            file=sys.stderr,
        )
        return 2
    if not isinstance(document, dict):
        print(
            f"check_release: report {args.report!r} is not a JSON object",
            file=sys.stderr,
        )
        return 2

    result = check_release(gate, document)
    _print_result(gate, result)
    return 0 if result.passed else 1


def _print_result(gate: ReleaseGate, result: GateResult) -> None:
    print(f"gate_version={gate.gate_version}")
    print("release gate: PASSED" if result.passed else "release gate: FAILED")
    for failure in result.failures:
        print(f"  - {failure}")
    tolerances = result.details.get("tolerances", {})
    for name, check in tolerances.items():
        print(
            f"  {name}: {check['status']} "
            f"(baseline={_rate_text(check.get('baseline'))} "
            f"report={_rate_text(check.get('report'))} "
            f"{check.get('bound')}={_rate_text(check.get('limit'))})"
        )


def _attestation(value: object) -> str:
    """报告声明归一:True → passed,False → failed,缺失/None → not_attested。"""
    if value is True:
        return ATTESTED_PASSED
    if value is False:
        return ATTESTED_FAILED
    return ATTESTED_MISSING


def _tolerance(
    *,
    baseline: float | None,
    report_value: object,
    tolerance: float,
    bound: str,
) -> dict[str, Any]:
    """单条容差比较;基线或报告值缺失(None/非数字)时跳过并注明原因。"""
    report_rate: float | None = None
    if is_metric_number(report_value):
        report_rate = float(report_value)
    if baseline is None or report_rate is None:
        return {
            "baseline": baseline,
            "report": report_value,
            "tolerance": tolerance,
            "bound": bound,
            "status": "skipped",
            "reason": "baseline or report value is unavailable (None)",
        }
    if bound == "floor":
        limit = baseline - tolerance
        regression = report_rate < limit
    else:
        limit = baseline + tolerance
        regression = report_rate > limit
    return {
        "baseline": baseline,
        "report": report_rate,
        "tolerance": tolerance,
        "bound": bound,
        "limit": limit,
        "status": "regression" if regression else "pass",
    }


def _mapping(value: object) -> dict[str, Any]:
    """报告分节归一:非 dict(缺失/非法)一律按空节处理。"""
    return dict(value) if isinstance(value, dict) else {}


def _rate_text(value: object) -> str:
    if is_metric_number(value):
        return f"{value:.4f}"
    return "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
