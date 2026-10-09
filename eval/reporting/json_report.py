"""JSON 报告构造(计划 05 Task 7 Step 3)。

:class:`ReportBuilder` 把 :class:`~eval.results.CaseResult`(执行捕获)与
:class:`~eval.scorers.composite_result.ScoredCaseResult`(结构化判分)组装
为一份 JSON 报告。语义约定:

- **分项、无综合总分**:metrics 只含任务完成/行为决策/写安全/故障恢复的
  分项比率与计数——维度之间不可互相折抵,报告层绝不生成 ``overall_score``;
- **分母真实性**:任务完成率分母 = ``included_in_quality_score=True`` 的
  Agent/Runtime 执行数;已启动后失败的 ``FAILED`` 计为未完成。基础设施错误、
  环境创建前失败与显式 ``SKIPPED`` 不入分母。安全、过程、依据等指标只在
  场景声明了对应判分项时计入各自分母;与 CaseResult 成对出现的判分只在配对
  结果计入质量分时贡献分子,未配对的判分按其自身计入(它来自一次真实
  完成的执行);
- **每例轨迹引用**:每例给出 run_id、脱敏后的事件 payload 摘要、ToolCall
  结论(stable_key/ordinal/status/failure_code,不含参数值)与 Operation
  结论——是脱敏摘要 + 引用,不嵌完整未脱敏轨迹;
- **凭据脱敏**:整份报告在返回前经
  :class:`runtime.observability.redaction.TraceRedactor` 双层脱敏(敏感键
  名 + ``sk-...``/``Bearer ...`` 密钥形态),Provider Key、Authorization
  Header 与未脱敏 Secret 绝不入报告。

``build`` 的关键字参数与计划调用形态兼容:``report_builder.build(results)``
单独调用时使用缺省(空指纹/无额外基础设施错误/不含配置段)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eval.config import EvalConfig
from eval.results import CaseResult
from eval.scorers.composite_result import ScoredCaseResult
from runtime.observability.redaction import TraceRedactor


REPORT_SCHEMA_VERSION: int = 3
"""报告结构版本:字段语义或结构变化时递增。"""


class ReportBuilder:
    """分项 JSON 报告构造器;构造时注入脱敏器(缺省双层脱敏)。"""

    def __init__(self, redactor: TraceRedactor | None = None) -> None:
        self._redactor = redactor if redactor is not None else TraceRedactor()

    def build(
        self,
        results: Sequence[ScoredCaseResult | CaseResult],
        *,
        protocol_fingerprint: str = "",
        infrastructure_errors: Sequence[Mapping[str, Any]] | None = None,
        config: EvalConfig | None = None,
    ) -> dict[str, Any]:
        """组装分项报告并整体脱敏;返回 JSON 可序列化的 dict。"""
        case_results = [r for r in results if isinstance(r, CaseResult)]
        scored = [r for r in results if isinstance(r, ScoredCaseResult)]
        by_key = {(r.case_id, r.repetition): r for r in case_results}

        included_keys = {
            key for key, result in by_key.items() if result.included_in_quality_score
        }
        standalone_scored_keys = {
            (s.case_id, s.repetition) for s in scored
        } - set(by_key)
        denominator = len(included_keys) + len(standalone_scored_keys)
        # 判分只在计入质量分时贡献分子;未配对判分视为其自身计入。
        contributing = [
            s
            for s in scored
            if (s.case_id, s.repetition) in included_keys
            or (s.case_id, s.repetition) in standalone_scored_keys
        ]

        report: dict[str, Any] = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "product_suite_eval",
            "protocol_fingerprint": protocol_fingerprint,
            "config": config.model_dump(mode="json") if config is not None else None,
            "totals": self._totals(case_results, scored, denominator),
            "metrics": self._metrics(contributing, denominator),
            "cases": self._cases(case_results, scored, by_key),
            "infrastructure_errors": self._infrastructure_errors(
                case_results, infrastructure_errors
            ),
        }
        redacted: dict[str, Any] = self._redactor.redact(report)
        return redacted

    # -- 分节组装 ---------------------------------------------------------

    @staticmethod
    def _totals(
        case_results: list[CaseResult],
        scored: list[ScoredCaseResult],
        denominator: int,
    ) -> dict[str, Any]:
        statuses = [result.status for result in case_results]
        return {
            "scenarios": len({r.case_id for r in case_results}
                             | {s.case_id for s in scored}),
            "executions": len(case_results),
            "completed": statuses.count("COMPLETED"),
            "failed": statuses.count("FAILED"),
            "infrastructure_errors": statuses.count("INFRASTRUCTURE_ERROR"),
            "skipped": statuses.count("SKIPPED"),
            "scored": len(scored),
            "protocol_valid": "SKIPPED" not in statuses,
            "quality_denominator": denominator,
        }

    @staticmethod
    def _metrics(
        contributing: list[ScoredCaseResult], denominator: int
    ) -> dict[str, Any]:
        task_passed = sum(1 for score in contributing if _product_task_passed(score))
        task_results_by_case: dict[str, list[bool]] = {}
        for score in contributing:
            task_results_by_case.setdefault(score.case_id, []).append(
                _product_task_passed(score)
            )
        stable_passed = sum(
            1 for results in task_results_by_case.values() if results and all(results)
        )
        safety_applicable = [s for s in contributing if s.safety.applicable]
        dangerous_attempt_cases = sum(
            1 for s in safety_applicable if s.safety.dangerous_attempts > 0
        )
        dangerous_side_effect_cases = sum(
            1 for s in safety_applicable if s.safety.actual_dangerous_side_effects > 0
        )
        process_applicable = [s for s in contributing if s.process.applicable]
        grounding_applicable = [s for s in contributing if s.grounding.applicable]
        process_passed = sum(1 for s in process_applicable if s.process.passed is True)
        grounding_passed = sum(
            1 for s in grounding_applicable if s.grounding.passed is True
        )
        return {
            "task_completion_rate": _rate(task_passed, denominator),
            "scenario_stability": {
                "total_scenarios": len(task_results_by_case),
                "stable_passed_scenarios": stable_passed,
                "stable_pass_rate": _rate(stable_passed, len(task_results_by_case)),
            },
            "dangerous_attempt_rate": _rate(
                dangerous_attempt_cases, len(safety_applicable)
            ),
            "dangerous_side_effect_rate": _rate(
                dangerous_side_effect_cases, len(safety_applicable)
            ),
            "counts": {
                "task_passed": task_passed,
                "dangerous_attempts": sum(
                    s.safety.dangerous_attempts for s in contributing
                ),
                "prevented_attempts": sum(
                    s.safety.prevented_attempts for s in contributing
                ),
                "actual_dangerous_side_effects": sum(
                    s.safety.actual_dangerous_side_effects for s in contributing
                ),
            },
            "denominators": {
                "included_in_quality_score": denominator,
                "safety_applicable_cases": len(safety_applicable),
            },
            "diagnostics": {
                "process_correctness_rate": _rate(process_passed, len(process_applicable)),
                "answer_grounding_rate": _rate(grounding_passed, len(grounding_applicable)),
            },
        }

    @staticmethod
    def _cases(
        case_results: list[CaseResult],
        scored: list[ScoredCaseResult],
        by_key: dict[tuple[str, int], CaseResult],
    ) -> list[dict[str, Any]]:
        entries: dict[tuple[str, int], dict[str, Any]] = {}
        for result in case_results:
            entries[(result.case_id, result.repetition)] = {
                "case_id": result.case_id,
                "repetition": result.repetition,
                "status": result.status,
                "included_in_quality_score": result.included_in_quality_score,
                "fingerprint": result.fingerprint,
                "error": result.error,
                "trace": _trace(result),
                "scores": None,
            }
        for s in scored:
            key = (s.case_id, s.repetition)
            entry = entries.setdefault(
                key,
                {
                    "case_id": s.case_id,
                    "repetition": s.repetition,
                    "status": None,
                    # 未配对判分来自一次真实完成的执行,按计入质量分处理。
                    "included_in_quality_score": key not in by_key,
                    "fingerprint": None,
                    "error": None,
                    "trace": None,
                    "scores": None,
                },
            )
            entry["scores"] = {
                "task": s.task.model_dump(mode="json"),
                "decision": s.decision.model_dump(mode="json"),
                "process": s.process.model_dump(mode="json"),
                "grounding": s.grounding.model_dump(mode="json"),
                "safety": s.safety.model_dump(mode="json"),
                "recovery": s.recovery.model_dump(mode="json"),
                "scorer_version": s.scorer_version,
            }
        return [entries[key] for key in sorted(entries)]

    @staticmethod
    def _infrastructure_errors(
        case_results: list[CaseResult],
        extra: Sequence[Mapping[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = [dict(entry) for entry in (extra or ())]
        for result in case_results:
            if result.status == "INFRASTRUCTURE_ERROR":
                errors.append(
                    {
                        "case_id": result.case_id,
                        "repetition": result.repetition,
                        "error": result.error,
                    }
                )
        return errors


def _trace(result: CaseResult) -> dict[str, Any]:
    """单例脱敏轨迹引用:run_id + 事件摘要 + ToolCall/Operation 结论。"""
    return {
        "run_id": str(result.run_id) if result.run_id is not None else None,
        "event_count": len(result.events),
        "events": [
            {
                "sequence_no": event.sequence_no,
                "event_type": event.event_type,
                "payload": dict(event.payload),
            }
            for event in sorted(result.events, key=lambda item: item.sequence_no)
        ],
        "tool_calls": [
            {
                "stable_key": call.stable_key,
                "ordinal": call.ordinal,
                "status": call.status,
                "failure_code": call.failure_code,
            }
            for call in sorted(result.tool_calls, key=lambda item: item.ordinal)
        ],
        "operation_count": len(result.operations),
        "operations": [
            {
                "status": operation.status,
                "adapter_key": operation.adapter_key,
                "logical_writes": operation.logical_writes,
            }
            for operation in result.operations
        ],
        "final_state": (
            result.final_state.model_dump(mode="json")
            if result.final_state is not None
            else None
        ),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    """真实分母比率;分母为 0 时返回 ``None``(不虚增、不猜测)。"""
    if denominator == 0:
        return None
    return numerator / denominator


def _product_task_passed(score: ScoredCaseResult) -> bool:
    """商品任务成功需满足状态判分及所有适用的过程/回答证据约束。"""
    return (
        score.task.passed
        and score.process.passed is not False
        and score.grounding.passed is not False
    )


__all__ = ["REPORT_SCHEMA_VERSION", "ReportBuilder"]
