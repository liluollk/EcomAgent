"""ContextBudgetPolicy、丢弃次序与 Summary 压缩语义测试(计划 02 Task 5 Step 1/Step 4)。"""

import json
from collections.abc import AsyncIterator

import pytest

from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendEvent,
    BackendFailed,
    BackendRequest,
    CancellationToken,
)
from runtime.interfaces.state import AssistantMessageStatus, MessageType
from runtime.orchestration.context import (
    ContextAssembler,
    ConversationMessage,
    ConversationView,
    MemoryItem,
    ToolResultProjection,
)
from runtime.orchestration.context_budget import (
    BudgetDecisionStatus,
    BudgetItem,
    ContextBudgetExceeded,
    ContextBudgetPolicy,
    ContextItemKind,
    ModelBudget,
)
from runtime.orchestration.summarization import (
    ConversationSummarizer,
    canonical_summary_source_hash,
)
from runtime.orchestration.token_estimator import HeuristicTokenEstimator


PREV_SUMMARY_PAYLOAD_JSON = json.dumps(
    {
        "schema_version": 1,
        "summary": "digest of messages 1-3",
        "key_facts": ["fact-one"],
    }
)


async def test_protected_context_over_budget_fails_closed(context_assembler, session) -> None:
    with pytest.raises(ContextBudgetExceeded, match="CONTEXT_BUDGET_EXCEEDED"):
        await context_assembler.build(session, input_budget_tokens=10)


def test_input_budget_subtracts_output_tool_schema_and_margin() -> None:
    budget = ModelBudget(
        context_window_tokens=128_000,
        max_output_tokens=8_192,
        tool_schema_tokens=6_000,
        safety_margin_tokens=4_000,
    )
    policy = ContextBudgetPolicy(HeuristicTokenEstimator(), model_budget=budget)
    assert budget.input_budget() == 128_000 - 8_192 - 6_000 - 4_000
    assert policy.input_budget() == budget.input_budget()


def test_heuristic_estimator_is_deterministic_and_conservative() -> None:
    estimator = HeuristicTokenEstimator()
    assert estimator.estimate("") == 0
    assert estimator.estimate("abcd") == 1
    assert estimator.estimate("abcde") == 2
    assert estimator.estimate("abcde") == estimator.estimate("abcde")


def _budget_items() -> list[BudgetItem]:
    return [
        BudgetItem(key="safety", kind=ContextItemKind.SAFETY_RULES, content="r" * 100),
        BudgetItem(key="hist-1", kind=ContextItemKind.HISTORY, content="h" * 50, sequence_no=1),
        BudgetItem(key="hist-2", kind=ContextItemKind.HISTORY, content="h" * 50, sequence_no=2),
        BudgetItem(key="tool-1", kind=ContextItemKind.TOOL_RESULT, content="t" * 50),
        BudgetItem(key="mem-high", kind=ContextItemKind.MEMORY, content="m" * 50, relevance=0.9),
        BudgetItem(key="mem-low", kind=ContextItemKind.MEMORY, content="n" * 50, relevance=0.1),
        BudgetItem(key="user", kind=ContextItemKind.CURRENT_USER_MESSAGE, content="u" * 10),
    ]


def test_eviction_order_memory_then_tool_projection_then_summary() -> None:
    policy = ContextBudgetPolicy(HeuristicTokenEstimator())
    items = _budget_items()
    budget = policy.estimate_items(items) - 120

    first = policy.evaluate(items, budget)
    assert first.status is BudgetDecisionStatus.EVICT
    assert first.evict_keys == ("mem-low",)

    items = [item for item in items if item.key not in first.evict_keys]
    second = policy.evaluate(items, budget)
    assert second.status is BudgetDecisionStatus.EVICT
    assert second.evict_keys == ("tool-1",)

    items = [item for item in items if item.key not in second.evict_keys]
    third = policy.evaluate(items, budget)
    assert third.status is BudgetDecisionStatus.SUMMARIZE_REQUIRED


def test_policy_fails_closed_when_only_protected_items_remain() -> None:
    policy = ContextBudgetPolicy(HeuristicTokenEstimator())
    items = [
        BudgetItem(key="safety", kind=ContextItemKind.SAFETY_RULES, content="r" * 100),
        BudgetItem(key="user", kind=ContextItemKind.CURRENT_USER_MESSAGE, content="u" * 20),
    ]
    decision = policy.evaluate(items, budget=10)
    assert decision.status is BudgetDecisionStatus.PROTECTED_OVER_BUDGET


async def test_summarizer_input_excludes_evicted_memory_and_tool_content(
    context_assembler, summary_backend
) -> None:
    view = ConversationView(
        messages=tuple(
            ConversationMessage(
                sequence_no=n,
                role=MessageType.USER if n % 2 == 1 else MessageType.ASSISTANT,
                content=f"history message {n}: " + "h" * 90,
                status=AssistantMessageStatus.COMPLETED if n % 2 == 0 else None,
            )
            for n in range(1, 4)
        ),
        memories=(
            MemoryItem(
                memory_id="mem-stale",
                content="stale memory: secret-alpha " + "m" * 70,
                relevance=0.1,
            ),
        ),
        tool_result_projections=(
            ToolResultProjection(
                call_id="call-old",
                tool_name="search_products",
                content="old tool result: secret-beta " + "t" * 70,
                ordinal=1,
            ),
        ),
        current_user_message=ConversationMessage(
            sequence_no=4, role=MessageType.USER, content="current intent: finalize"
        ),
    )

    request = await context_assembler.build(view, input_budget_tokens=500)

    assert summary_backend.requests, "summary generation should have been triggered"
    assert request.session_summary is not None
    summarizer_input = "\n".join(
        message.content for message in summary_backend.requests[0].messages
    )
    assert "secret-alpha" not in summarizer_input
    assert "secret-beta" not in summarizer_input
    assert "history message 1" in summarizer_input


def _summary_history_message(sequence_no: int) -> ConversationMessage:
    return ConversationMessage(
        sequence_no=sequence_no,
        role=MessageType.USER if sequence_no % 2 == 1 else MessageType.ASSISTANT,
        content=f"history message {sequence_no}: " + "p" * 110,
        status=AssistantMessageStatus.COMPLETED if sequence_no % 2 == 0 else None,
    )


async def test_summary_incremental_merge_submits_only_uncovered_segment(
    summary_backend,
    summarizer,
    token_estimator,
    safety_rules,
    agent_instructions,
    workspace_summary,
) -> None:
    history = tuple(_summary_history_message(n) for n in range(1, 9))
    persisted_summary = ConversationMessage(
        sequence_no=9,
        role=MessageType.SESSION_SUMMARY,
        content=PREV_SUMMARY_PAYLOAD_JSON,
        covered_from_sequence=1,
        covered_to_sequence=3,
        summary_schema_version=1,
        source_hash="hash-of-1-3",
    )
    view = ConversationView(
        messages=history + (persisted_summary,),
        current_user_message=ConversationMessage(
            sequence_no=10,
            role=MessageType.USER,
            content="current intent: finish the report",
        ),
    )
    observed_calls: list[int] = []
    assembler = ContextAssembler(
        safety_rules=safety_rules,
        agent_instructions=agent_instructions,
        workspace_summary=workspace_summary,
        summarizer=summarizer,
        estimator=token_estimator,
        model_call_observer=observed_calls.append,
    )

    request = await assembler.build(view, input_budget_tokens=520)

    assert len(summary_backend.requests) == 3
    # 禁用 Tool Calling:Summary 请求不携带任何 Tool Definition。
    assert all(req.tool_definitions == () for req in summary_backend.requests)
    first_body = "\n".join(
        message.content for message in summary_backend.requests[0].messages
    )
    assert "digest of messages 1-3" in first_body
    assert "history message 4" in first_body
    assert "history message 5" in first_body
    assert "history message 1" not in first_body
    assert "history message 6" not in first_body
    full_history_size = sum(len(message.content) for message in history)
    assert len(first_body) < full_history_size
    assert observed_calls
    assert all(tokens > 0 for tokens in observed_calls)

    summary = request.session_summary
    assert summary is not None
    assert summary.covered_from_sequence == 1
    assert summary.covered_to_sequence == 8
    assert summary.schema_version == 1
    assert summary.source_hash == canonical_summary_source_hash(history)
    assert summary.source_hash != "hash-of-1-3"
    assert summary.provider_role == "user"


async def test_summary_schema_validation_failure_fails_closed(
    context_assembler, summary_backend, long_session
) -> None:
    summary_backend.payload_json = "this is not json"
    with pytest.raises(ContextBudgetExceeded, match="CONTEXT_BUDGET_EXCEEDED"):
        await context_assembler.build(long_session, input_budget_tokens=800)


class FailingSummaryBackend:
    """以 BackendFailed 事件失败的后端(Provider 故障注入)。"""

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(capability_keys=frozenset({"streaming"}))

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        yield BackendFailed(error_code="PROVIDER_OVERLOAD", retryable=True)


async def test_summary_backend_failure_fails_closed(
    token_estimator, safety_rules, agent_instructions, workspace_summary, long_session
) -> None:
    summarizer = ConversationSummarizer(
        backend=FailingSummaryBackend(),
        estimator=token_estimator,
        input_budget_tokens=400,
    )
    assembler = ContextAssembler(
        safety_rules=safety_rules,
        agent_instructions=agent_instructions,
        workspace_summary=workspace_summary,
        summarizer=summarizer,
        estimator=token_estimator,
    )
    with pytest.raises(ContextBudgetExceeded, match="CONTEXT_BUDGET_EXCEEDED"):
        await assembler.build(long_session, input_budget_tokens=800)
