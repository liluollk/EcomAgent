"""Agent 执行步骤判分。"""

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult
from eval.schema import Scenario


class ProcessScore(BaseModel):
    """仅对场景明确声明的必要先后步骤判分，不强制唯一工具路径。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applicable: bool
    passed: bool | None
    checked_constraints: int
    violations: tuple[str, ...] = ()


class ProcessScorer:
    def __init__(self, scenario: Scenario) -> None:
        self._constraints = scenario.tool_order_constraints

    def score(self, result: CaseResult) -> ProcessScore:
        if not self._constraints:
            return ProcessScore(
                applicable=False, passed=None, checked_constraints=0
            )
        calls = sorted(
            result.tool_calls,
            key=lambda call: (
                call.sequence_no if call.sequence_no is not None else call.ordinal
            ),
        )
        positions: dict[str, list[int]] = {}
        for index, call in enumerate(calls):
            positions.setdefault(call.stable_key, []).append(index)

        violations: list[str] = []
        for constraint in self._constraints:
            before = positions.get(constraint.before, [])
            after = positions.get(constraint.after, [])
            if not before or not after:
                violations.append(
                    f"missing_required_order:{constraint.before}->{constraint.after}"
                )
            elif min(before) >= min(after):
                violations.append(
                    f"wrong_order:{constraint.before}->{constraint.after}"
                )
        return ProcessScore(
            applicable=True,
            passed=not violations,
            checked_constraints=len(self._constraints),
            violations=tuple(violations),
        )


__all__ = ["ProcessScore", "ProcessScorer"]
