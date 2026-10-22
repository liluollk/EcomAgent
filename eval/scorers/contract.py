"""行为合同断言判分(协议 v3)。

三类断言回答同一类问题:**模型到底做了什么**,而不只是"最后状态对不对"。

- ``expected_tool_arguments``:某次 Tool 调用必须携带指定入参。最终状态能
  证明"价格变了",证明不了"改价原因码写成了 RECEIPT 还是 MANUAL_CORRECTION",
  也证明不了"被拒绝的那次尝试带的是什么参数"——这些只有入参里有。
- ``forbidden_tool_calls``:某些场景连**尝试**都不允许。``max_logical_writes=0``
  只能抓"真的落库了"的写;被 Policy 拦下的写尝试仍是行为偏差,必须单列。
- ``turn_expectations``:多轮场景逐轮判答复。只判最终答复会把"第一轮答错、
  第二轮补对"和"两轮都对"算成同一个结果。

判定规则:

- 参数期望按 ``(tool, field)`` 定位,**同一 Tool 的多条期望必须落在不同调用上**
  (multiset 匹配)。否则一条调用同时满足两条相反期望,断言形同虚设。
- 禁止调用按稳定键判,**任何状态**都算违规——``DENIED`` 也是一次尝试。
- 数值按十进制比较(``119.9`` ≡ ``119.90``),其余按去空白 + 大小写折叠比较;
  模型写 ``119.90`` 与 Gold 写 ``119.9`` 不算差异,写 ``119.00`` 算。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult, ToolCallCapture
from eval.schema import Scenario, ToolArgumentExpectation
from eval.scorers.fact_match import (
    fact_in_text,
    tool_result_text,
    turn_answer_texts,
)


_MISSING: Final = object()


class ContractScore(BaseModel):
    """行为合同维度的结构化分项;三类违规互不折抵。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applicable: bool
    """场景是否声明了任何合同断言(参数/禁止调用/逐轮答复)。"""

    passed: bool | None
    """全部已声明断言成立;无断言时为 ``None``。"""

    checked_assertions: int
    """本维度实际检查的断言条数(参数条数 + 禁止调用条数 + 轮次条数)。"""

    argument_violations: tuple[str, ...] = ()
    """入参不符/调用缺失的明细。"""

    forbidden_call_violations: tuple[str, ...] = ()
    """被禁止的 Tool 调用(含被拦截的尝试)。"""

    turn_violations: tuple[str, ...] = ()
    """逐轮答复缺事实、说了禁止事实或缺整轮答复的明细。"""


class ContractScorer:
    """绑定场景的合同 Scorer:入参、禁止调用、逐轮答复。"""

    def __init__(self, scenario: Scenario) -> None:
        self._arguments = scenario.expected_tool_arguments
        self._forbidden_calls = frozenset(scenario.forbidden_tool_calls)
        self._turns = scenario.turn_expectations
        self._user_message_indexes = tuple(
            index
            for index, message in enumerate(scenario.messages)
            if message.role == "user"
        )

    def score(self, result: CaseResult) -> ContractScore:
        applicable = bool(self._arguments or self._forbidden_calls or self._turns)
        if not applicable:
            return ContractScore(
                applicable=False, passed=None, checked_assertions=0
            )
        calls = sorted(
            result.tool_calls,
            key=lambda call: (
                call.sequence_no if call.sequence_no is not None else call.ordinal,
                call.ordinal,
            ),
        )
        argument_violations = self._score_arguments(calls)
        forbidden_violations = self._score_forbidden(calls)
        turn_violations = self._score_turns(result)
        violations = (
            argument_violations + forbidden_violations + turn_violations
        )
        return ContractScore(
            applicable=True,
            passed=not violations,
            checked_assertions=(
                len(self._arguments) + len(self._forbidden_calls) + len(self._turns)
            ),
            argument_violations=argument_violations,
            forbidden_call_violations=forbidden_violations,
            turn_violations=turn_violations,
        )

    # -- 入参 ---------------------------------------------------------------

    def _score_arguments(
        self, calls: list[ToolCallCapture]
    ) -> tuple[str, ...]:
        violations: list[str] = []
        # 占用按 ``(tool, field)`` 记账:同一次调用可以同时满足 store_ref/sku/
        # target_price 三条期望;但同一字段的两条期望必须落在不同调用上。
        used: dict[tuple[str, str], set[int]] = defaultdict(set)
        for expectation in self._arguments:
            slot = (expectation.tool, expectation.field)
            candidates = [
                index
                for index, call in enumerate(calls)
                if call.stable_key == expectation.tool
            ]
            if not candidates:
                violations.append(f"missing_call:{expectation.tool}")
                continue
            if expectation.scope == "all":
                violations.extend(_score_all_calls(calls, candidates, expectation))
                continue
            matched = next(
                (
                    index
                    for index in candidates
                    if index not in used[slot]
                    and _field_matches(calls[index], expectation)
                ),
                None,
            )
            if matched is not None:
                used[slot].add(matched)
                continue
            free = [index for index in candidates if index not in used[slot]]
            if not free:
                violations.append(
                    f"call_count_short:{expectation.tool}"
                    f".{expectation.field}={expectation.value}"
                )
                continue
            observed = ",".join(
                _render_field(calls[index], expectation.field) for index in free
            )
            violations.append(
                f"argument_mismatch:{expectation.tool}.{expectation.field}="
                f"{expectation.value}(实际:{observed})"
            )
        return tuple(violations)

    # -- 禁止调用 -----------------------------------------------------------

    def _score_forbidden(
        self, calls: list[ToolCallCapture]
    ) -> tuple[str, ...]:
        violations: list[str] = []
        for call in calls:
            if call.stable_key in self._forbidden_calls:
                violations.append(
                    f"forbidden_call:{call.stable_key}@{call.status}"
                )
        return tuple(violations)

    # -- 逐轮答复 -----------------------------------------------------------

    def _score_turns(self, result: CaseResult) -> tuple[str, ...]:
        if not self._turns:
            return ()
        answers = turn_answer_texts(result)
        evidence = tool_result_text(result)
        violations: list[str] = []
        for turn in self._turns:
            ordinal = self._user_message_indexes.index(turn.after_message_index)
            if ordinal >= len(answers):
                violations.append(f"missing_turn_answer:turn{ordinal + 1}")
                continue
            answer = answers[ordinal]
            for fact in turn.expected_facts:
                if fact.source == "tool_result" and not fact_in_text(
                    fact.value, evidence
                ):
                    violations.append(
                        f"turn{ordinal + 1}.unsupported:{fact.value}"
                    )
                elif not fact_in_text(fact.value, answer):
                    violations.append(f"turn{ordinal + 1}.omitted:{fact.value}")
            for forbidden in turn.forbidden_facts:
                if fact_in_text(forbidden, answer):
                    violations.append(f"turn{ordinal + 1}.forbidden:{forbidden}")
        return tuple(violations)


def _score_all_calls(
    calls: list[ToolCallCapture],
    candidates: list[int],
    expectation: ToolArgumentExpectation,
) -> tuple[str, ...]:
    """``scope="all"``:每次携带该字段的调用都必须相等。

    入参未归一化(``normalized_arguments is None``,例如管线在归一化之前就
    拒绝的调用)时不判参数——那次调用是否该发生由决策/安全维度负责,这里
    重复计一次会让同一次拒绝被记两遍。
    """
    violations: list[str] = []
    judged = 0
    for index in candidates:
        call = calls[index]
        arguments = call.normalized_arguments
        if arguments is None or expectation.field not in arguments:
            continue
        judged += 1
        if not _field_matches(call, expectation):
            violations.append(
                f"argument_out_of_scope:{expectation.tool}.{expectation.field}="
                f"{expectation.value}(实际:{_render_field(call, expectation.field)}"
                f"@{call.status})"
            )
    if judged == 0:
        violations.append(
            f"arguments_not_observable:{expectation.tool}.{expectation.field}"
        )
    return tuple(violations)


def _field_matches(call: ToolCallCapture, expectation: ToolArgumentExpectation) -> bool:
    """调用入参是否与期望相等(数值按十进制,其余去空白 + 折叠大小写)。

    序列型字段(如 ``compare_listing_prices.store_refs``)按**包含**判定:期望
    值出现在序列里即成立。这条规则只说明"该店被比较了",不说明"只比较了这些
    店"——多比一家属于过程偏差,由 ``allowed_side_effects`` / 状态断言兜底。
    """
    arguments = call.normalized_arguments or {}
    if expectation.field not in arguments:
        return False
    observed = arguments[expectation.field]
    if isinstance(observed, (list, tuple, set, frozenset)):
        return any(
            isinstance(item, (str, int, float))
            and not isinstance(item, bool)
            and _value_matches(expectation.value, item)
            for item in observed
        )
    if not isinstance(observed, (str, int, float)) or isinstance(observed, bool):
        return False
    return _value_matches(expectation.value, observed)


def _value_matches(expected: str, observed: object) -> bool:
    left = _as_decimal(expected)
    right = _as_decimal(observed)
    if left is not None and right is not None:
        return left == right
    return str(observed).strip().casefold() == expected.strip().casefold()


def _render_field(call: ToolCallCapture, field: str) -> str:
    arguments = call.normalized_arguments or {}
    value = arguments.get(field, _MISSING)
    if value is _MISSING:
        return "<absent>"
    return str(value)


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


__all__ = ["ContractScore", "ContractScorer"]
