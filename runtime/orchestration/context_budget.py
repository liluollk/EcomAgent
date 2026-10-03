"""Context 输入预算与丢弃策略(计划 02 Task 5 Step 3;架构文档 §7)。

预算公式:输入预算 = Context Window − 最大输出 − Tool Schema 估算 − 安全余量。

Protected 集合(永不裁剪、永不摘要):Runtime 安全规则、Agent Prompt/Skill 指令、
Workspace 非敏感摘要、持久化 Summary、当前用户消息、待处理 Input/Confirmation
Request(含 fingerprint)与恢复所需事实。

超预算时的收敛次序(架构文档 §7):
  低相关长期记忆 → 旧 ToolResult 投影 → 对较早已完成消息生成 Summary;
仍有缺口且无 History 可摘要(即 Protected 自身超限)→ ``ContextBudgetExceeded``
fail closed,不静默丢内容。Summary Schema 校验失败同样 fail closed(见
``summarization`` 与 ``ContextAssembler``)。

本模块是纯评估器:只产出决策,条目的实际移除由 ContextAssembler 执行。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from runtime.orchestration.token_estimator import TokenEstimator


class ContextBudgetExceeded(RuntimeError):
    """Protected Context 超限或 Summary 校验失败;``str`` 含 CONTEXT_BUDGET_EXCEEDED。"""

    def __init__(self, detail: str) -> None:
        super().__init__(f"CONTEXT_BUDGET_EXCEEDED: {detail}")
        self.detail = detail


@dataclass(frozen=True)
class ModelBudget:
    """模型配置的硬限制(ContextBudgetPolicy 的输入;架构文档 §7)。"""

    context_window_tokens: int
    max_output_tokens: int
    tool_schema_tokens: int
    safety_margin_tokens: int

    def input_budget(self) -> int:
        """本轮输入预算 = Context Window − 最大输出 − Tool Schema − 安全余量。"""
        budget = (
            self.context_window_tokens
            - self.max_output_tokens
            - self.tool_schema_tokens
            - self.safety_margin_tokens
        )
        if budget < 0:
            raise ValueError("model budget components exceed the context window")
        return budget


class ContextItemKind(str, Enum):
    """预算条目种类;决定 Protected 属性与丢弃次序。"""

    SAFETY_RULES = "SAFETY_RULES"
    AGENT_INSTRUCTIONS = "AGENT_INSTRUCTIONS"
    WORKSPACE_SUMMARY = "WORKSPACE_SUMMARY"
    PERSISTED_SUMMARY = "PERSISTED_SUMMARY"
    HISTORY = "HISTORY"
    TOOL_RESULT = "TOOL_RESULT"
    MEMORY = "MEMORY"
    PENDING_REQUEST = "PENDING_REQUEST"
    CURRENT_USER_MESSAGE = "CURRENT_USER_MESSAGE"


_PROTECTED_KINDS = frozenset(
    {
        ContextItemKind.SAFETY_RULES,
        ContextItemKind.AGENT_INSTRUCTIONS,
        ContextItemKind.WORKSPACE_SUMMARY,
        ContextItemKind.PERSISTED_SUMMARY,
        ContextItemKind.PENDING_REQUEST,
        ContextItemKind.CURRENT_USER_MESSAGE,
    }
)

# 直接丢弃次序:先丢低相关 Memory,再丢旧 ToolResult 投影;
# HISTORY 不直接丢弃,只能被 Summary 替代(SUMMARIZE_REQUIRED)。
_DROP_RANKS: dict[ContextItemKind, int] = {
    ContextItemKind.MEMORY: 1,
    ContextItemKind.TOOL_RESULT: 2,
}


@dataclass(frozen=True)
class BudgetItem:
    """参与预算计算的最小条目;``payload`` 携带种类专属的不透明数据。

    - HISTORY 条目的 ``payload`` 是来源 :class:`ConversationMessage`;
    - PERSISTED_SUMMARY 条目的 ``payload`` 是 :class:`SummaryResult`;
    - ``name`` / ``call_id`` 供 ToolResult 投影映射为 tool Message。
    """

    key: str
    kind: ContextItemKind
    content: str
    sequence_no: int | None = None
    relevance: float = 1.0
    name: str | None = None
    call_id: str | None = None
    payload: Any = None

    @property
    def is_protected(self) -> bool:
        """安全规则、当前意图、待处理 Request 等永不裁剪(架构文档 §7)。"""
        return self.kind in _PROTECTED_KINDS

    @property
    def drop_rank(self) -> int | None:
        """直接丢弃次序;None 表示不可直接丢弃(只能被 Summary 替代)。"""
        return _DROP_RANKS.get(self.kind)


class BudgetDecisionStatus(str, Enum):
    """预算评估结论。"""

    FITS = "FITS"
    EVICT = "EVICT"
    SUMMARIZE_REQUIRED = "SUMMARIZE_REQUIRED"
    PROTECTED_OVER_BUDGET = "PROTECTED_OVER_BUDGET"


@dataclass(frozen=True)
class BudgetDecision:
    """评估结论;``evict_keys`` 仅在 EVICT 时非空。"""

    status: BudgetDecisionStatus
    evict_keys: tuple[str, ...] = ()


class ContextBudgetPolicy:
    """纯函数式预算评估器(架构文档 §7:ContextBudgetPolicy)。

    每次调用只给出下一步收敛动作;调用方执行后重新评估,直至 FITS 或
    fail closed。这样"先丢 Memory、再丢 ToolResult、最后生成 Summary"的
    次序由决策序列天然表达并可测试。
    """

    def __init__(
        self,
        estimator: TokenEstimator,
        *,
        low_relevance_threshold: float = 0.5,
        model_budget: ModelBudget | None = None,
    ) -> None:
        if not 0.0 <= low_relevance_threshold <= 1.0:
            raise ValueError("low_relevance_threshold must be within [0, 1]")
        self._estimator = estimator
        self._low_relevance_threshold = low_relevance_threshold
        self._model_budget = model_budget

    def input_budget(self) -> int | None:
        """默认输入预算;未配置 ModelBudget 时返回 None(不设限)。"""
        return self._model_budget.input_budget() if self._model_budget is not None else None

    def estimate(self, text: str) -> int:
        return self._estimator.estimate(text)

    def estimate_items(self, items: Sequence[BudgetItem]) -> int:
        return sum(self._estimator.estimate(item.content) for item in items)

    def evaluate(self, items: Sequence[BudgetItem], budget: int) -> BudgetDecision:
        """对当前条目集合给出下一步收敛决策(见模块 docstring 的次序)。"""
        total = self.estimate_items(items)
        if total <= budget:
            return BudgetDecision(BudgetDecisionStatus.FITS)

        low_relevance = sorted(
            (
                item
                for item in items
                if item.kind is ContextItemKind.MEMORY
                and item.relevance < self._low_relevance_threshold
            ),
            key=lambda item: item.relevance,
        )
        if low_relevance:
            return BudgetDecision(
                BudgetDecisionStatus.EVICT, evict_keys=tuple(i.key for i in low_relevance)
            )

        tool_results = tuple(
            item.key for item in items if item.kind is ContextItemKind.TOOL_RESULT
        )
        if tool_results:
            return BudgetDecision(BudgetDecisionStatus.EVICT, evict_keys=tool_results)

        if any(item.kind is ContextItemKind.HISTORY for item in items):
            return BudgetDecision(BudgetDecisionStatus.SUMMARIZE_REQUIRED)

        return BudgetDecision(BudgetDecisionStatus.PROTECTED_OVER_BUDGET)


__all__ = [
    "BudgetDecision",
    "BudgetDecisionStatus",
    "BudgetItem",
    "ContextBudgetExceeded",
    "ContextBudgetPolicy",
    "ContextItemKind",
    "ModelBudget",
]
