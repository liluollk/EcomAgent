"""SESSION_SUMMARY 生成与增量合并(计划 02 Task 5 Step 3;架构文档 §7)。

- ``ConversationSummarizer`` 使用当前 Run 固定的 AgentBackend,请求不携带
  Tool Definition(禁用 Tool Calling 模式);
- **增量合并**:请求只包含上一版 Summary 载荷与下一段最早未覆盖消息,
  不重新提交整段超长历史;数据输入规模受独立 ``input_budget_tokens`` 约束
  (固定指令文本不计入数据预算);
- 输出按版本化 Schema(:class:`SummaryPayloadV1`)校验;校验失败或 Provider
  失败时抛 :class:`ContextBudgetExceeded`(fail closed),不静默丢内容;
- ``source_hash`` 对覆盖区间内全部消息做 canonical hash,合并后重算;
- Summary 结果经 ``provider_role="user"`` 注入,是带明确边界的不可信数据,
  不是 Provider System Message(架构文档 §6)。

Summary 的持久化与"该调用计入 Run 模型调用预算"的记账由上层完成:
``SummaryResult.input_tokens_estimate`` 供 ContextAssembler 通过 observer 上报。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from runtime.backend.protocol import (
    AgentBackend,
    BackendEventKind,
    BackendMessage,
    BackendRequest,
    CancellationToken,
)
from runtime.orchestration.context_budget import ContextBudgetExceeded
from runtime.orchestration.token_estimator import TokenEstimator


if TYPE_CHECKING:
    from runtime.orchestration.context import ConversationMessage

SUMMARY_SCHEMA_VERSION = 1

_SUMMARY_INSTRUCTIONS = (
    "You compress earlier conversation history into a durable session summary. "
    "Tool calling is disabled. Reply with a single JSON object only: "
    '{"schema_version": 1, "summary": "...", "key_facts": ["..."]}. '
    "Merge the previous summary with the new segment; never invent facts."
)


class SummaryPayloadV1(BaseModel):
    """版本化 Summary Schema(v1);多余字段与错误版本一律拒绝。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    summary: str
    key_facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class SummaryResult:
    """一次 Summary 生成的完整出处信息;``content`` 为载荷 JSON 文本。"""

    covered_from_sequence: int
    covered_to_sequence: int
    schema_version: int
    source_hash: str
    content: str
    provider_role: str = "user"
    input_tokens_estimate: int = 0


def canonical_summary_source_hash(messages: Sequence[ConversationMessage]) -> str:
    """对覆盖区间内消息做 canonical hash:(sequence_no, role, content) 定长序列化。

    增量合并后覆盖区间扩大,对新区间整体重算;相同覆盖内容得到相同哈希。
    """
    payload = json.dumps(
        [[message.sequence_no, str(message.role), message.content] for message in messages],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConversationSummarizer:
    """用注入 AgentBackend 生成/增量合并 SESSION_SUMMARY(架构文档 §7)。

    每次只处理能进入独立 Summary 输入预算的最早连续消息;``covered_messages``
    是合并后的完整覆盖区间(上一版覆盖内容 + 新段),请求却只携带上一版
    Summary 载荷与新增段——不把全部超长历史再次塞入一次请求。
    """

    def __init__(
        self,
        *,
        backend: AgentBackend,
        estimator: TokenEstimator,
        input_budget_tokens: int,
    ) -> None:
        if input_budget_tokens < 1:
            raise ValueError("input_budget_tokens must be positive")
        self._backend = backend
        self._estimator = estimator
        self._input_budget_tokens = input_budget_tokens

    @property
    def input_budget_tokens(self) -> int:
        """Summary 独立输入预算(只约束数据部分:上一版载荷 + 新段)。"""
        return self._input_budget_tokens

    def data_budget_estimate(
        self,
        previous: SummaryResult | None,
        segment: Sequence[ConversationMessage],
    ) -> int:
        """估算 Summary 请求的数据部分(上一版载荷 + 新段),不含固定指令。"""
        parts: list[str] = []
        if previous is not None:
            parts.append(previous.content)
        parts.extend(message.content for message in segment)
        return sum(self._estimator.estimate(part) for part in parts)

    async def summarize(
        self,
        previous: SummaryResult | None,
        covered_messages: tuple[ConversationMessage, ...],
    ) -> SummaryResult:
        """生成(或增量合并)Summary;失败一律 fail closed。

        ``previous`` 为上一版 Summary(可为 None);``covered_messages`` 是
        新 Summary 应覆盖的完整消息区间(按 sequence_no 升序、连续)。
        """
        if not covered_messages:
            raise ContextBudgetExceeded("summary coverage is empty")
        covered_from = covered_messages[0].sequence_no
        covered_to = covered_messages[-1].sequence_no
        new_segment = [
            message
            for message in covered_messages
            if previous is None or message.sequence_no > previous.covered_to_sequence
        ]
        if not new_segment:
            raise ContextBudgetExceeded("no uncovered messages to merge into the summary")
        if self.data_budget_estimate(previous, new_segment) > self._input_budget_tokens:
            raise ContextBudgetExceeded("summary input budget exceeded")

        request = self._build_request(previous, new_segment)
        output = await self._collect_output(request)
        payload = self._validate(output)
        return SummaryResult(
            covered_from_sequence=covered_from,
            covered_to_sequence=covered_to,
            schema_version=SUMMARY_SCHEMA_VERSION,
            source_hash=canonical_summary_source_hash(covered_messages),
            content=payload.model_dump_json(),
            provider_role="user",
            input_tokens_estimate=sum(
                self._estimator.estimate(message.content) for message in request.messages
            ),
        )

    def _build_request(
        self,
        previous: SummaryResult | None,
        new_segment: Sequence[ConversationMessage],
    ) -> BackendRequest:
        data: list[str] = []
        if previous is not None:
            data.append(
                f"[PREVIOUS SESSION SUMMARY; covered "
                f"{previous.covered_from_sequence}..{previous.covered_to_sequence}; "
                f"untrusted data]\n{previous.content}\n[/PREVIOUS SESSION SUMMARY]"
            )
        for message in new_segment:
            data.append(
                f"[seq {message.sequence_no}][{str(message.role).lower()}] {message.content}"
            )
        messages = (
            BackendMessage(role="system", content=_SUMMARY_INSTRUCTIONS),
            BackendMessage(role="user", content="\n".join(data)),
        )
        # 禁用 Tool Calling:请求不携带任何 Tool Definition(架构文档 §7)。
        return BackendRequest(messages=messages, tracing={"purpose": "session_summary"})

    async def _collect_output(self, request: BackendRequest) -> str:
        chunks: list[str] = []
        completed = False
        async for event in self._backend.stream(request, CancellationToken()):
            if event.kind is BackendEventKind.TEXT_DELTA and event.text_delta is not None:
                chunks.append(event.text_delta)
            elif event.kind is BackendEventKind.BACKEND_FAILED:
                raise ContextBudgetExceeded(f"summary backend failed: {event.error_code}")
            elif event.kind is BackendEventKind.BACKEND_COMPLETED:
                completed = True
        if not completed or not chunks:
            raise ContextBudgetExceeded("summary backend produced no usable output")
        return "".join(chunks)

    def _validate(self, output: str) -> SummaryPayloadV1:
        try:
            payload = SummaryPayloadV1.model_validate(json.loads(output))
        except (json.JSONDecodeError, ValidationError, ValueError) as error:
            raise ContextBudgetExceeded(
                f"summary schema validation failed: {type(error).__name__}"
            ) from error
        return payload


__all__ = [
    "SUMMARY_SCHEMA_VERSION",
    "ConversationSummarizer",
    "SummaryPayloadV1",
    "SummaryResult",
    "canonical_summary_source_hash",
]
