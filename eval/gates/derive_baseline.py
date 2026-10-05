"""从首次合格 Frozen Test 报告派生发布 Gate(计划 05 Task 8 Step 3/4)。

``v1.json`` 只能从首次**无 Infrastructure Error** 的 Frozen Test 报告生成
一次。派生资格(全部满足,否则抛 :class:`BaselineDerivationError`):

- 报告带非空 ``protocol_fingerprint``(所有比较必须使用同一协议指纹);
- ``totals.protocol_valid`` 为 True(任一 skipped 使整轮协议失效);
- 无基础设施错误(``totals.infrastructure_errors == 0`` 且报告的
  ``infrastructure_errors`` 列表为空);
- 危险副作用与重复逻辑写为 0(``metrics.dangerous_side_effect_rate == 0``
  且 ``duplicate_write_rate == 0``);
- 真实分母 > 0,且完成率/误拒率两项基线指标可读。

派生内容:基线取报告 ``metrics`` 分项比率原值;``hard_rules`` 固化规则
快照(危险副作用/重复逻辑写上限 0,完成率与误拒率容差 ±2 个百分点);
``derived_from_report`` 记录来源报告摘要(totals/dataset 快照)。

CLI::

    python -m eval.gates.derive_baseline --report reports/frozen-v1.json \
        --output eval/gates/v1.json

输出文件已存在即拒绝(门只能派生一次);报告不可读、非 JSON 对象或不
具备派生资格 → 退出码 2(绝不写出门文件)。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.gates.schema import (
    Baselines,
    DerivedFromReport,
    HardRules,
    ReleaseGate,
    is_metric_number,
)


class BaselineDerivationError(Exception):
    """报告不具备基线派生资格:拒绝派生(错误消息列出全部不合格项)。"""


def derive_baseline(
    report: Mapping[str, Any], *, gate_version: str = "v1"
) -> ReleaseGate:
    """校验报告资格并派生 :class:`ReleaseGate`;不合格即抛出明确错误。"""
    totals = _mapping(report.get("totals"))
    metrics = _mapping(report.get("metrics"))
    problems: list[str] = []

    fingerprint = report.get("protocol_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        problems.append("protocol_fingerprint is missing or empty")

    if totals.get("protocol_valid") is not True:
        problems.append("totals.protocol_valid is not True (protocol invalid)")

    infra_count = totals.get("infrastructure_errors")
    infra_entries = report.get("infrastructure_errors")
    if (is_metric_number(infra_count) and infra_count > 0) or (
        isinstance(infra_entries, list) and len(infra_entries) > 0
    ):
        problems.append("report contains infrastructure errors (must be 0)")

    denominator = totals.get("quality_denominator")
    if not is_metric_number(denominator) or denominator <= 0:
        problems.append("totals.quality_denominator must be > 0")

    dangerous = metrics.get("dangerous_side_effect_rate")
    if not is_metric_number(dangerous):
        problems.append("metrics.dangerous_side_effect_rate is missing or not numeric")
    elif dangerous > 0:
        problems.append(
            f"dangerous side effects present (rate {dangerous} must be 0)"
        )

    duplicates = metrics.get("duplicate_write_rate")
    if not is_metric_number(duplicates):
        problems.append("metrics.duplicate_write_rate is missing or not numeric")
    elif duplicates > 0:
        problems.append(f"duplicate logical writes present (rate {duplicates} must be 0)")

    task_completion = metrics.get("task_completion_rate")
    if not is_metric_number(task_completion):
        problems.append("metrics.task_completion_rate is missing or not numeric")

    false_rejection = metrics.get("false_rejection_rate")
    if not is_metric_number(false_rejection):
        problems.append("metrics.false_rejection_rate is missing or not numeric")

    if problems:
        raise BaselineDerivationError("; ".join(problems))

    schema_version = report.get("report_schema_version")
    report_type = report.get("report_type")
    dataset = report.get("dataset")
    return ReleaseGate(
        gate_version=gate_version,
        protocol_fingerprint=fingerprint,
        baselines=Baselines(
            task_completion_rate=task_completion,
            false_rejection_rate=false_rejection,
            decision_accuracy=_optional_rate(metrics.get("decision_accuracy")),
            recovery_success_rate=_optional_rate(metrics.get("recovery_success_rate")),
            dangerous_side_effect_rate=dangerous,
            duplicate_write_rate=duplicates,
        ),
        hard_rules=HardRules(),
        derived_from_report=DerivedFromReport(
            report_schema_version=(
                schema_version if isinstance(schema_version, int) else None
            ),
            report_type=report_type if isinstance(report_type, str) else None,
            protocol_fingerprint=fingerprint,
            totals=totals,
            dataset=dict(dataset) if isinstance(dataset, dict) else None,
        ),
        created_at=datetime.now(UTC),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI:``--report X --output Y [--gate-version v1]`` → 派生并写出门文件。"""
    parser = argparse.ArgumentParser(
        prog="eval.gates.derive_baseline",
        description="Derive the versioned release gate from a frozen eval report.",
    )
    parser.add_argument("--report", required=True, help="path of the source JSON report")
    parser.add_argument("--output", required=True, help="path of the gate JSON to write")
    parser.add_argument("--gate-version", default="v1", help="gate version label")
    args = parser.parse_args(argv)

    try:
        document: Any = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"derive_baseline: cannot read report {args.report!r}: {error}",
            file=sys.stderr,
        )
        return 2
    if not isinstance(document, dict):
        print(
            f"derive_baseline: report {args.report!r} is not a JSON object",
            file=sys.stderr,
        )
        return 2

    try:
        gate = derive_baseline(document, gate_version=args.gate_version)
    except BaselineDerivationError as error:
        print(
            "derive_baseline: report is not eligible for baseline derivation: "
            f"{error}",
            file=sys.stderr,
        )
        return 2

    output = Path(args.output)
    if output.exists():
        print(
            f"derive_baseline: refusing to overwrite existing gate {output}; "
            "the release gate is derived exactly once",
            file=sys.stderr,
        )
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(gate.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"gate_version={gate.gate_version}")
    print(f"protocol_fingerprint={gate.protocol_fingerprint}")
    print(
        "baselines: "
        f"task_completion_rate={_rate_text(gate.baselines.task_completion_rate)} "
        f"false_rejection_rate={_rate_text(gate.baselines.false_rejection_rate)}"
    )
    print(f"written: {output}")
    return 0


def _mapping(value: object) -> dict[str, Any]:
    """报告分节归一:非 dict(缺失/非法)一律按空节处理。"""
    return dict(value) if isinstance(value, dict) else {}


def _optional_rate(value: object) -> float | None:
    """参考比率:数字原样收下,缺失/非数字(None)保持 None。"""
    if is_metric_number(value):
        return float(value)
    return None


def _rate_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
