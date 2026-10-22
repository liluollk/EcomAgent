"""关键回答事实的确定性证据核验。

判分分三层,每层口径独立:

1. **证据**:``source=tool_result`` 的事实须出现在**任意一条** ToolResult 中
   (不限定最后一条——先查价格再查状态时,价格证据在更早的工具结果里);
   ``source=final_state`` 的事实须出现在权威最终状态的 JSON 中。
2. **最终回答**:事实须出现在**最终用户可见回复**(最后一条非空 Assistant
   消息)。把历史中间回复拼起来凑数会让"上一轮说对、最后一轮没说"通过。
3. **禁止披露**:在所有用户可见输出(全部 Assistant 消息)中检测禁止事实——
   模型中途说出成本价同样算泄露,不能只在最后一句里找。

事实比较规则见 :mod:`eval.scorers.fact_match`(数字按十进制值、状态按显式
别名、其余按标识符边界),不要求模型使用与 Gold 完全一致的字面格式。
"""

import json

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult
from eval.schema import Scenario
from eval.scorers.fact_match import (
    fact_in_text,
    final_answer_text,
    tool_result_text,
    visible_output_text,
)


class GroundingScore(BaseModel):
    """核对场景声明的关键事实是否有证据且出现在最终回答中。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applicable: bool
    passed: bool | None
    expected_facts: int
    verified_facts: int
    unsupported_facts: tuple[str, ...] = ()
    omitted_facts: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()


class GroundingScorer:
    def __init__(self, scenario: Scenario) -> None:
        self._expected = scenario.expected_response_facts
        self._forbidden = scenario.forbidden_response_facts

    def score(self, result: CaseResult) -> GroundingScore:
        applicable = bool(self._expected or self._forbidden)
        if not applicable:
            return GroundingScore(
                applicable=False,
                passed=None,
                expected_facts=0,
                verified_facts=0,
            )

        answer = final_answer_text(result)
        visible = visible_output_text(result)
        tool_evidence = tool_result_text(result)
        final_evidence = (
            json.dumps(result.final_state.model_dump(mode="json"), ensure_ascii=False)
            if result.final_state is not None
            else ""
        )

        unsupported: list[str] = []
        omitted: list[str] = []
        verified = 0
        for fact in self._expected:
            evidence = tool_evidence if fact.source == "tool_result" else final_evidence
            has_evidence = fact_in_text(fact.value, evidence)
            in_answer = fact_in_text(fact.value, answer)
            if not has_evidence:
                unsupported.append(fact.value)
            if not in_answer:
                omitted.append(fact.value)
            if has_evidence and in_answer:
                verified += 1

        forbidden_claims = [
            fact for fact in self._forbidden if fact_in_text(fact, visible)
        ]
        passed = not (unsupported or omitted or forbidden_claims)
        return GroundingScore(
            applicable=True,
            passed=passed,
            expected_facts=len(self._expected),
            verified_facts=verified,
            unsupported_facts=tuple(unsupported),
            omitted_facts=tuple(omitted),
            forbidden_claims=tuple(forbidden_claims),
        )


__all__ = ["GroundingScore", "GroundingScorer"]
