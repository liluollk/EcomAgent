"""从首次合格 Core 报告派生发布 Gate。

``v1.json`` 只能从首次**无 Infrastructure Error** 的 Core 报告生成
一次。派生资格(全部满足,否则抛 :class:`BaselineDerivationError`):

- 报告带非空 ``benchmark_fingerprint``(所有比较必须使用同一评测基准);
- ``totals.protocol_valid`` 为 True(任一 skipped 使整轮协议失效);
- 无基础设施错误(``totals.infrastructure_errors == 0`` 且报告的
  ``infrastructure_errors`` 列表为空);
- ``dataset.split`` 为 ``core``;
- 真实分母 > 0,且任务完成率基线可读;
- **逐例结果完整**:每个 case 的重复次数一致且 > 0;给了 ``--dataset`` 时
  报告的场景集合必须与冻结数据集完全一致——少跑一个场景会让分母变小、
  完成率虚高,不能冻成基线;
- **全部用例通过**——默认拒绝把带失败的版本冻成基线;确实要接受已知失败时
  必须显式 ``allow_known_failures=True``,失败项会写进
  ``derived_from_report.known_failures``,不能被静默覆盖。

派生内容:基线取报告 ``metrics`` 分项比率原值;``hard_rules`` 固化规则
快照(任务完成率容差 ±2 个百分点,并要求报告声明 Runtime/E2E 通过、附带
独立 Safety 报告);``case_outcomes`` 冻结逐例通过结果,``case_passes``
冻结逐例通过次数(已知失败从 2/3 恶化到 1/3 也要能被发现),供后续 Gate
逐例比对,避免平均分掩盖单例退化;``derived_from_report`` 记录来源报告摘要
(totals/dataset 快照)与显式接受的失败项。

CLI::

    python -m eval.gates.derive_baseline --report reports/core-v1.json \
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

from eval.gates.check_release import case_outcomes, case_passes
from eval.gates.schema import (
    Baselines,
    DerivedFromReport,
    HardRules,
    ReleaseGate,
    is_metric_number,
)


def _execution_counts(report: Mapping[str, Any]) -> dict[str, int]:
    """报告里每个 case 的执行次数(用于重复次数与场景完整性校验)。"""
    counts: dict[str, int] = {}
    cases = report.get("cases")
    if not isinstance(cases, list):
        return counts
    for case in cases:
        if not isinstance(case, Mapping):
            continue
        case_id = case.get("case_id")
        if isinstance(case_id, str):
            counts[case_id] = counts.get(case_id, 0) + 1
    return counts


class BaselineDerivationError(Exception):
    """报告不具备基线派生资格:拒绝派生(错误消息列出全部不合格项)。"""


def derive_baseline(
    report: Mapping[str, Any],
    *,
    gate_version: str = "v1",
    allow_known_failures: bool = False,
    expected_case_ids: Sequence[str] | None = None,
    require_safety_evidence: bool = True,
) -> ReleaseGate:
    """校验报告资格并派生 :class:`ReleaseGate`;不合格即抛出明确错误。

    ``expected_case_ids``(通常来自冻结数据集)用于校验报告是否真的跑全了:
    报告少跑一整个场景,完成率会因分母变小而虚高,必须拒绝。
    """
    totals = _mapping(report.get("totals"))
    metrics = _mapping(report.get("metrics"))
    outcomes = case_outcomes(report)
    passes = case_passes(report)
    known_failures = tuple(
        sorted(case_id for case_id, passed in outcomes.items() if not passed)
    )
    problems: list[str] = []

    fingerprint = report.get("benchmark_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        problems.append("benchmark_fingerprint is missing or empty")

    if totals.get("protocol_valid") is not True:
        problems.append("totals.protocol_valid is not True (protocol invalid)")

    dataset = report.get("dataset")
    if not isinstance(dataset, dict) or dataset.get("split") != "core":
        problems.append("baseline report must come from the core split")

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

    if known_failures and not allow_known_failures:
        problems.append(
            "core report has failing cases ("
            + ", ".join(known_failures)
            + "); pass allow_known_failures to freeze them explicitly"
        )

    # -- 逐例完整性:分母变小会让完成率虚高 ------------------------------
    execution_counts = _execution_counts(report)
    repetitions: int | None = None
    if not execution_counts:
        problems.append("report contains no per-case results")
    else:
        distinct = set(execution_counts.values())
        if len(distinct) != 1:
            problems.append(
                "report has uneven repetition counts per case ("
                + ", ".join(
                    f"{case_id}={count}"
                    for case_id, count in sorted(execution_counts.items())
                )
                + ")"
            )
        else:
            repetitions = distinct.pop()
            if repetitions <= 0:
                problems.append("report has zero executions for some case")
    if expected_case_ids is not None:
        expected = set(expected_case_ids)
        reported = set(execution_counts)
        missing = sorted(expected - reported)
        unexpected = sorted(reported - expected)
        if missing:
            problems.append("report is missing dataset cases: " + ", ".join(missing))
        if unexpected:
            problems.append(
                "report contains cases outside the dataset: " + ", ".join(unexpected)
            )

    if problems:
        raise BaselineDerivationError("; ".join(problems))

    schema_version = report.get("report_schema_version")
    report_type = report.get("report_type")
    dataset = report.get("dataset")
    return ReleaseGate(
        gate_version=gate_version,
        benchmark_fingerprint=fingerprint,
        baselines=Baselines(
            task_completion_rate=task_completion,
        ),
        hard_rules=HardRules(require_safety_evidence=require_safety_evidence),
        case_outcomes=outcomes,
        case_passes=passes,
        repetitions=repetitions,
        scenario_count=len(outcomes),
        system_fingerprint=(
            report.get("system_fingerprint")
            if isinstance(report.get("system_fingerprint"), str)
            else None
        ),
        derived_from_report=DerivedFromReport(
            report_schema_version=(
                schema_version if isinstance(schema_version, int) else None
            ),
            report_type=report_type if isinstance(report_type, str) else None,
            benchmark_fingerprint=fingerprint,
            totals=totals,
            dataset=dict(dataset) if isinstance(dataset, dict) else None,
            known_failures=known_failures,
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
    parser.add_argument(
        "--allow-known-failures",
        action="store_true",
        help="freeze a baseline that still has failing cases (recorded in the gate)",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="path of the frozen dataset YAML; when given, the report must "
        "cover exactly these case ids",
    )
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

    expected_case_ids: list[str] | None = None
    if args.dataset is not None:
        from pathlib import Path as _Path

        from eval.dataset_loader import load_dataset

        try:
            expected_case_ids = [
                scenario.id for scenario in load_dataset(_Path(args.dataset))
            ]
        except (OSError, ValueError) as error:
            print(
                f"derive_baseline: cannot load dataset {args.dataset!r}: {error}",
                file=sys.stderr,
            )
            return 2

    try:
        gate = derive_baseline(
            document,
            gate_version=args.gate_version,
            allow_known_failures=args.allow_known_failures,
            expected_case_ids=expected_case_ids,
        )
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
    print(f"benchmark_fingerprint={gate.benchmark_fingerprint}")
    print(f"task_completion_rate={_rate_text(gate.baselines.task_completion_rate)}")
    if gate.derived_from_report.known_failures:
        print(
            "known_failures=" + ", ".join(gate.derived_from_report.known_failures)
        )
    print(f"written: {output}")
    return 0


def _mapping(value: object) -> dict[str, Any]:
    """报告分节归一:非 dict(缺失/非法)一律按空节处理。"""
    return dict(value) if isinstance(value, dict) else {}


def _rate_text(value: float) -> str:
    return f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
