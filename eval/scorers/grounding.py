"""关键回答事实的确定性证据核验。"""

import json

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult
from eval.schema import Scenario


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

        assistant = "\n".join(
            message.content
            for message in result.conversation_messages
            if message.role.upper() == "ASSISTANT"
        )
        latest_tool_message = max(
            (
                message
                for message in result.conversation_messages
                if message.role.upper() == "TOOL"
            ),
            key=lambda message: message.sequence_no,
            default=None,
        )
        tool_evidence = latest_tool_message.content if latest_tool_message else ""
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
            has_evidence = fact.value in evidence
            in_answer = fact.value in assistant
            if not has_evidence:
                unsupported.append(fact.value)
            if not in_answer:
                omitted.append(fact.value)
            if has_evidence and in_answer:
                verified += 1

        forbidden_claims = [fact for fact in self._forbidden if fact in assistant]
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
