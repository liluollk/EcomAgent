"""从首次合格 Held-out 报告派生发布 Gate。

``v1.json`` 只能从首次**无 Infrastructure Error** 的 Held-out 报告生成
一次。派生资格(全部满足,否则抛 :class:`BaselineDerivationError`):

- 报告带非空 ``protocol_fingerprint``(所有比较必须使用同一协议指纹);
- ``totals.protocol_valid`` 为 True(任一 skipped 使整轮协议失效);
- 无基础设施错误(``totals.infrastructure_errors == 0`` 且报告的
  ``infrastructure_errors`` 列表为空);
- ``dataset.split`` 为 ``held_out``;
- 真实分母 > 0,且任务完成率基线可读。

派生内容:基线取报告 ``metrics`` 分项比率原值;``hard_rules`` 固化规则
快照(任务完成率容差 ±2 个百分点);
``derived_from_report`` 记录来源报告摘要(totals/dataset 快照)。

CLI::

    python -m eval.gates.derive_baseline --report reports/held-out-v1.json \
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

    dataset = report.get("dataset")
    if not isinstance(dataset, dict) or dataset.get("split") != "held_out":
        problems.append("baseline report must come from the held_out split")

    infra_count = totals.get("infrastructure_errors")
    infra_entries = report.get("infrastructure_errors")
    if (is_metric_number(infra_count) and infra_count > 0) or (
        isinstance(infra_entries, list) and len(infra_entries) > 0
    ):
        problems.append("report contains infrastructure errors (must be 0)")

    denominator = totals.get("quality_denominator")
    if not is_metric_number(denominator) or denominator <= 0:
        problems.append("totals.quality_denominator must be > 0")

    task_completion = metrics.get("task_completion_rate")
    if not is_metric_number(task_completion):
        problems.append("metrics.task_completion_rate is missing or not numeric")

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
    print(f"task_completion_rate={_rate_text(gate.baselines.task_completion_rate)}")
    print(f"written: {output}")
    return 0


def _mapping(value: object) -> dict[str, Any]:
    """报告分节归一:非 dict(缺失/非法)一律按空节处理。"""
    return dict(value) if isinstance(value, dict) else {}


def _rate_text(value: float) -> str:
    return f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
