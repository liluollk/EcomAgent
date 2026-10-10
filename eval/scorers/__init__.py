"""权威状态 Scorer(计划 05 Task 3)。

按维度独立判分,判分只消费 :class:`~eval.results.CaseResult` 捕获的权威
痕迹(ToolCall/Operation/AgentEvent/权威最终状态),对照
:class:`~eval.schema.Scenario` 的显式期望;模型文本宣称不参与任何判分:

- :class:`~eval.scorers.task_completion.TaskCompletionScorer`:任务完成
  (权威状态一致 + 逻辑写不超限);
- :class:`~eval.scorers.decision.DecisionScorer`:行为决策(允许/拒绝 vs
  场景副作用类别,含合法任务误拒);
- :class:`~eval.scorers.safety.SafetyScorer`:写安全(危险尝试 / 被拦截
  尝试 / 实际危险副作用);
- :class:`~eval.scorers.recovery.RecoveryScorer`:故障恢复(UNKNOWN 回查
  路径、正确处理、重复逻辑写);
- :func:`~eval.scorers.composite_result.score_case`:组装结构化分项,
  不生成综合总分。
"""

from eval.scorers.composite_result import ScoredCaseResult, score_case
from eval.scorers.decision import DecisionScore, DecisionScorer
from eval.scorers.grounding import GroundingScore, GroundingScorer
from eval.scorers.process import ProcessScore, ProcessScorer
from eval.scorers.recovery import RecoveryScore, RecoveryScorer
from eval.scorers.safety import SafetyScore, SafetyScorer
from eval.scorers.task_completion import TaskCompletionScore, TaskCompletionScorer


__all__ = [
    "DecisionScore",
    "DecisionScorer",
    "GroundingScore",
    "GroundingScorer",
    "ProcessScore",
    "ProcessScorer",
    "RecoveryScore",
    "RecoveryScorer",
    "SafetyScore",
    "SafetyScorer",
    "ScoredCaseResult",
    "TaskCompletionScore",
    "TaskCompletionScorer",
    "score_case",
]
