"""结构化判分结果组装(计划 05 Task 3)。

:class:`ScoredCaseResult` 只组装各维度分项(任务完成、行为决策、写安全、
故障恢复)与标识字段(``case_id``/``repetition``/``scorer_version``),
**不生成任何综合总分**——维度之间不可互相折抵,报告层(Task 7)也只
呈现分项比率,评分折算被显式排除在体系之外。
"""

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult
from eval.schema import Scenario
from eval.scorers.contract import ContractScore, ContractScorer
from eval.scorers.decision import DecisionScore, DecisionScorer
from eval.scorers.grounding import GroundingScore, GroundingScorer
from eval.scorers.process import ProcessScore, ProcessScorer
from eval.scorers.recovery import RecoveryScore, RecoveryScorer
from eval.scorers.safety import SafetyScore, SafetyScorer
from eval.scorers.task_completion import TaskCompletionScore, TaskCompletionScorer


class ScoredCaseResult(BaseModel):
    """单 case 的结构化判分结果(无综合总分,分项不可折抵)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    repetition: int
    scorer_version: str
    task: TaskCompletionScore
    contract: ContractScore
    decision: DecisionScore
    process: ProcessScore
    grounding: GroundingScore
    safety: SafetyScore
    recovery: RecoveryScore


def score_case(
    scenario: Scenario, result: CaseResult, *, scorer_version: str
) -> ScoredCaseResult:
    """以各维度 Scorer 判分单个捕获结果并组装结构化分项。

    ``scorer_version`` 应来自 :class:`~eval.config.EvalConfig` 的固定配置,
    随结果记录以保证判分规则可追溯(协议漂移检测的组成部分)。
    """
    return ScoredCaseResult(
        case_id=result.case_id,
        repetition=result.repetition,
        scorer_version=scorer_version,
        task=TaskCompletionScorer(scenario).score(result),
        contract=ContractScorer(scenario).score(result),
        decision=DecisionScorer(scenario).score(result),
        process=ProcessScorer(scenario).score(result),
        grounding=GroundingScorer(scenario).score(result),
        safety=SafetyScorer(scenario).score(result),
        recovery=RecoveryScorer(scenario).score(result),
    )


__all__ = ["ScoredCaseResult", "score_case"]
