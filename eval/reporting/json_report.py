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

from eval.capability_manifest import EvalCapabilityManifest
from eval.config import EvalConfig
from eval.results import CaseResult
from eval.scorers.composite_result import ScoredCaseResult
from runtime.observability.redaction import TraceRedactor


REPORT_SCHEMA_VERSION: int = 8
"""报告结构版本:字段语义或结构变化时递增。

v5:新增 ``metrics.decision`` / ``metrics.recovery`` 顶层分项、安全披露与未决
危险调用两项比率(此前 DecisionScore/RecoveryScore 只出现在逐例结果里,
无法一眼比较版本间的误拒、误放行、恢复成功率与重复写)。
v6:口径拆开、分母外显——``metrics.state_completion_rate`` 只算权威状态,
``metrics.task_completion_rate`` 才是"状态 + 已声明的过程/依据约束";
``metrics.grounding`` / ``metrics.process`` 给出通过数与适用数,
``metrics.decision.decision_accuracy_by_decision`` 给决策级(而非逐例全对)
准确率;``assertion_coverage`` 说明本分区到底有多少场景声明了哪类断言,
避免"没断言"被读成"断言全过"。
v7:行为合同进报告——``metrics.contract``(入参/禁止调用/逐轮答复)独立成
维度,并计入任务完成的口径;``cases[].scores.contract`` 之外,轨迹里的
ToolCall 带出**脱敏后的实际入参**,使"改价原因码写错"这类缺陷可从报告直接
看出,不必回捞数据库。
v8:答复原文进报告——``trace.messages`` 按序号给出 USER/ASSISTANT 文本
(ToolResult 仅留开头)并标注是否截断,同时 ``metrics.clarification`` 独立
报告"缺信息场景是否先追问后写"。此前"答复依据未命中"只能记成"0 命中",
无法区分答错内容与答对但没用断言选的那个词。
"""

_EXCERPT_ROLES = frozenset({"USER", "ASSISTANT", "TOOL"})
_MESSAGE_EXCERPT_LIMIT: int = 1000
_TOOL_EXCERPT_LIMIT: int = 400


class ReportBuilder:
    """分项 JSON 报告构造器;构造时注入脱敏器(缺省双层脱敏)。"""

    def __init__(self, redactor: TraceRedactor | None = None) -> None:
        self._redactor = redactor if redactor is not None else TraceRedactor()

    def build(
        self,
        results: Sequence[ScoredCaseResult | CaseResult],
        *,
        protocol_fingerprint: str = "",
        benchmark_fingerprint: str = "",
        system_fingerprint: str = "",
        infrastructure_errors: Sequence[Mapping[str, Any]] | None = None,
        config: EvalConfig | None = None,
        capability_manifest: EvalCapabilityManifest | None = None,
        assertion_coverage: Mapping[str, Any] | None = None,
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
            "benchmark_fingerprint": benchmark_fingerprint,
            "system_fingerprint": system_fingerprint,
            "config": config.model_dump(mode="json") if config is not None else None,
            "capability_manifest": (
                capability_manifest.model_dump(mode="json")
                if capability_manifest is not None
                else None
            ),
            "assertion_coverage": dict(assertion_coverage or {}),
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
        state_passed = sum(1 for score in contributing if score.task.passed)
        decision_correct_decisions = sum(
            s.decision.correct_decisions for s in contributing
        )
        decision_total_decisions = sum(s.decision.total_decisions for s in contributing)
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
        disclosure_cases = sum(
            1 for s in safety_applicable if s.safety.disclosed_forbidden_facts
        )
        unresolved_cases = sum(
            1 for s in safety_applicable if s.safety.unresolved_dangerous_attempts > 0
        )
        process_applicable = [s for s in contributing if s.process.applicable]
        grounding_applicable = [s for s in contributing if s.grounding.applicable]
        process_passed = sum(1 for s in process_applicable if s.process.passed is True)
        grounding_passed = sum(
            1 for s in grounding_applicable if s.grounding.passed is True
        )
        contract_applicable = [s for s in contributing if s.contract.applicable]
        contract_passed = sum(
            1 for s in contract_applicable if s.contract.passed is True
        )
        clarify_applicable = [s for s in contributing if s.clarification.applicable]
        clarify_passed = sum(
            1 for s in clarify_applicable if s.clarification.passed is True
        )
        decision_applicable = [s for s in contributing if s.decision.total_decisions > 0]
        decision_correct = sum(
            1 for s in decision_applicable if s.decision.decision_accuracy is True
        )
        legal_write_decisions = sum(
            s.decision.legal_write_decisions for s in decision_applicable
        )
        forbidden_write_decisions = sum(
            s.decision.total_decisions - s.decision.legal_write_decisions
            for s in decision_applicable
        )
        false_rejections = sum(s.decision.false_rejections for s in decision_applicable)
        false_allowances = sum(s.decision.false_allowances for s in decision_applicable)
        recovery_applicable = [s for s in contributing if s.recovery.recovery_applicable]
        recovery_passed = sum(
            1 for s in recovery_applicable if s.recovery.recovery_success is True
        )
        unknown_encounters = sum(s.recovery.unknown_encounters for s in contributing)
        unknown_handled = sum(
            s.recovery.unknown_handled_correctly for s in contributing
        )
        return {
            "state_completion_rate": _rate(state_passed, denominator),
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
            "sensitive_disclosure_rate": _rate(
                disclosure_cases, len(safety_applicable)
            ),
            "unresolved_dangerous_call_rate": _rate(
                unresolved_cases, len(safety_applicable)
            ),
            "decision": {
                "applicable_cases": len(decision_applicable),
                "decision_accuracy_rate": _rate(
                    decision_correct, len(decision_applicable)
                ),
                "decision_accuracy_by_decision": _rate(
                    decision_correct_decisions, decision_total_decisions
                ),
                "correct_decisions": decision_correct_decisions,
                "total_decisions": decision_total_decisions,
                "false_rejection_rate": _rate(false_rejections, legal_write_decisions),
                "false_allowance_rate": _rate(
                    false_allowances, forbidden_write_decisions
                ),
            },
            "process": {
                "applicable_executions": len(process_applicable),
                "passed_executions": process_passed,
                "pass_rate": _rate(process_passed, len(process_applicable)),
            },
            "grounding": {
                "applicable_executions": len(grounding_applicable),
                "passed_executions": grounding_passed,
                "pass_rate": _rate(grounding_passed, len(grounding_applicable)),
            },
            "contract": {
                "applicable_executions": len(contract_applicable),
                "passed_executions": contract_passed,
                "pass_rate": _rate(contract_passed, len(contract_applicable)),
                "checked_assertions": sum(
                    s.contract.checked_assertions for s in contract_applicable
                ),
                "violating_executions": len(contract_applicable) - contract_passed,
                "arguments": {
                    "violating_executions": sum(
                        1
                        for s in contract_applicable
                        if s.contract.argument_violations
                    ),
                },
                "forbidden_calls": {
                    "violating_executions": sum(
                        1
                        for s in contract_applicable
                        if s.contract.forbidden_call_violations
                    ),
                    "attempts": sum(
                        len(s.contract.forbidden_call_violations)
                        for s in contract_applicable
                    ),
                },
                "turns": {
                    "violating_executions": sum(
                        1 for s in contract_applicable if s.contract.turn_violations
                    ),
                },
            },
            "clarification": {
                "applicable_executions": len(clarify_applicable),
                "passed_executions": clarify_passed,
                "pass_rate": _rate(clarify_passed, len(clarify_applicable)),
                "no_ask_before_supply": sum(
                    1
                    for s in clarify_applicable
                    if s.clarification.asked_before_supply is False
                ),
                "write_before_supply": sum(
                    1
                    for s in clarify_applicable
                    if s.clarification.write_before_supply is True
                ),
            },
            "recovery": {
                "applicable_cases": len(recovery_applicable),
                "recovery_success_rate": _rate(
                    recovery_passed, len(recovery_applicable)
                ),
                "unresolved_unknowns": max(0, unknown_encounters - unknown_handled),
                "duplicate_logical_writes": sum(
                    s.recovery.duplicate_logical_writes for s in contributing
                ),
            },
            "counts": {
                "task_passed": task_passed,
                "state_passed": state_passed,
                "grounding_applicable_executions": len(grounding_applicable),
                "process_applicable_executions": len(process_applicable),
                "contract_applicable_executions": len(contract_applicable),
                "contract_passed_executions": contract_passed,
                "contract_argument_violations": sum(
                    len(s.contract.argument_violations) for s in contract_applicable
                ),
                "contract_forbidden_calls": sum(
                    len(s.contract.forbidden_call_violations)
                    for s in contract_applicable
                ),
                "contract_turn_violations": sum(
                    len(s.contract.turn_violations) for s in contract_applicable
                ),
                "dangerous_attempts": sum(
                    s.safety.dangerous_attempts for s in contributing
                ),
                "prevented_attempts": sum(
                    s.safety.prevented_attempts for s in contributing
                ),
                "actual_dangerous_side_effects": sum(
                    s.safety.actual_dangerous_side_effects for s in contributing
                ),
                "unresolved_dangerous_attempts": sum(
                    s.safety.unresolved_dangerous_attempts for s in contributing
                ),
                "sensitive_disclosures": sum(
                    len(s.safety.disclosed_forbidden_facts) for s in contributing
                ),
                "false_rejections": false_rejections,
                "false_allowances": false_allowances,
                "unknown_encounters": unknown_encounters,
                "unresolved_unknowns": max(0, unknown_encounters - unknown_handled),
                "duplicate_logical_writes": sum(
                    s.recovery.duplicate_logical_writes for s in contributing
                ),
            },
            "denominators": {
                "included_in_quality_score": denominator,
                "safety_applicable_cases": len(safety_applicable),
                "decision_applicable_cases": len(decision_applicable),
                "recovery_applicable_cases": len(recovery_applicable),
                "grounding_applicable_cases": len(grounding_applicable),
                "process_applicable_cases": len(process_applicable),
                "contract_applicable_cases": len(contract_applicable),
                "clarification_applicable_cases": len(clarify_applicable),
                "legal_write_decisions": legal_write_decisions,
                "forbidden_write_decisions": forbidden_write_decisions,
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
                "contract": s.contract.model_dump(mode="json"),
                "clarification": s.clarification.model_dump(mode="json"),
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
    """单例脱敏轨迹引用:run_id + 事件摘要 + 会话消息 + ToolCall/Operation 结论。"""
    return {
        "run_id": str(result.run_id) if result.run_id is not None else None,
        "event_count": len(result.events),
        "messages": _message_excerpts(result),
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
                "arguments": (
                    dict(call.normalized_arguments)
                    if call.normalized_arguments is not None
                    else None
                ),
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


def _message_excerpts(result: CaseResult) -> list[dict[str, Any]]:
    """按序号输出会话消息摘要(脱敏后截断)。

    答复原文是失败归因的必要证据:没有它,"答复依据未命中"只能记成"0 命中",
    无法区分"答错了内容"和"答对了但没提到断言选的那个词"。ToolResult 通常
    是整段 JSON,只保留开头用于核对,避免报告体积随工具输出膨胀。
    """
    excerpts: list[dict[str, Any]] = []
    for message in sorted(result.conversation_messages, key=lambda item: item.sequence_no):
        role = message.role.upper()
        if role not in _EXCERPT_ROLES:
            continue
        limit = _TOOL_EXCERPT_LIMIT if role == "TOOL" else _MESSAGE_EXCERPT_LIMIT
        content = message.content
        excerpts.append(
            {
                "sequence_no": message.sequence_no,
                "role": role,
                "content": content[:limit],
                "truncated": len(content) > limit,
            }
        )
    return excerpts


def _rate(numerator: int, denominator: int) -> float | None:
    """真实分母比率;分母为 0 时返回 ``None``(不虚增、不猜测)。"""
    if denominator == 0:
        return None
    return numerator / denominator


def _product_task_passed(score: ScoredCaseResult) -> bool:
    """商品任务成功需满足状态判分与全部适用的行为约束(过程/依据/合同)。"""
    return (
        score.task.passed
        and score.process.passed is not False
        and score.grounding.passed is not False
        and score.contract.passed is not False
        and score.clarification.passed is not False
    )


__all__ = ["REPORT_SCHEMA_VERSION", "ReportBuilder"]
