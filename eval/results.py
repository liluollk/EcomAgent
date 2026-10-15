"""Eval 结果结构(计划 05 Task 2)。

- :class:`CaseResult`:单个 case/repetition 的完整捕获(状态、协议指纹、
  AgentEvent/ToolCall/Operation 捕获与权威最终状态),frozen 且可 JSON
  序列化,供 Scorer(Task 3)与报告(Task 7)消费;
- :class:`EvalRoundResult`:一轮评测的全部结果;``protocol_valid`` 表达
  协议有效性——任一 SKIPPED case 即整轮失效(计划 Step 3);
- :func:`by_case_id`:结果按 case_id 键控,内容为 :class:`CaseContent`
  可比较投影——剔除跨轮易变的标识(Run/Workspace UUID、参数中的资源
  ID 等),只保留可判定内容(状态、事件类型序列、ToolCall 结论、Operation
  结论与权威最终状态),使不同执行顺序产生的轮次可直接比较相等。

状态语义(deterministic 分类,见 eval.runner):
- ``COMPLETED``:用例端到端执行完成(不代表任务成功,判分由 Scorer 负责);
- ``INFRASTRUCTURE_ERROR``:Provider/连接/依赖失败,不计入质量分母;
- ``FAILED``:Agent/Runtime 执行失败;已建立运行环境时计入任务完成率失败分母;
- ``SKIPPED``:显式 skip 注入,不计入质量分母并使整轮协议失效。
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eval.schema import InventoryState, ListingState


CaseStatus = Literal["COMPLETED", "INFRASTRUCTURE_ERROR", "FAILED", "SKIPPED"]
"""Runner 级状态:反映执行健康度,不表达任务成败(判分属 Scorer)。"""


class EventCapture(BaseModel):
    """一条 AgentEvent 捕获(sequence_no + 类型 + 已脱敏 payload)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence_no: int
    event_type: str
    payload: dict[str, Any]


class MessageCapture(BaseModel):
    """会话中实际持久化的用户、Assistant 与 ToolResult 消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: uuid.UUID | None
    sequence_no: int
    role: str
    content: str


class ToolCallCapture(BaseModel):
    """一条 ToolCall 捕获(稳定键、序号、结论与规范化参数)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stable_key: str
    ordinal: int
    sequence_no: int | None = None
    status: str
    failure_code: str | None = None
    normalized_arguments: dict[str, Any] | None = None


class OperationCapture(BaseModel):
    """一条 Operation 捕获(平台逻辑写入记录)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    adapter_key: str
    logical_writes: int = 0


class FinalState(BaseModel):
    """权威最终状态:平台侧 Listing 状态(符号键)与逻辑写总次数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    listings: tuple[ListingState, ...] = ()
    inventory_levels: tuple[InventoryState, ...] = ()
    logical_writes: int = 0


class CaseResult(BaseModel):
    """单个 case/repetition 的完整捕获(frozen,可 JSON 序列化)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    repetition: int
    status: CaseStatus
    included_in_quality_score: bool
    fingerprint: str
    run_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    error: str | None = None
    events: tuple[EventCapture, ...] = ()
    conversation_messages: tuple[MessageCapture, ...] = ()
    tool_calls: tuple[ToolCallCapture, ...] = ()
    operations: tuple[OperationCapture, ...] = ()
    final_state: FinalState | None = None


class EvalRoundResult(BaseModel):
    """一轮评测的全部结果(顺序无关;经 :func:`by_case_id` 比较)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results: tuple[CaseResult, ...] = ()

    @property
    def protocol_valid(self) -> bool:
        """协议有效性:任一 SKIPPED case 即整轮失效。"""
        return all(result.status != "SKIPPED" for result in self.results)

    @property
    def completed(self) -> int:
        return sum(1 for result in self.results if result.status == "COMPLETED")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status == "FAILED")

    @property
    def infrastructure_errors(self) -> int:
        return sum(
            1 for result in self.results if result.status == "INFRASTRUCTURE_ERROR"
        )

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.status == "SKIPPED")


class ToolCallContent(BaseModel):
    """ToolCall 的可比较内容(不含资源 ID 等跨轮易变值)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stable_key: str
    ordinal: int
    status: str
    failure_code: str | None = None


class OperationContent(BaseModel):
    """Operation 的可比较内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    adapter_key: str


class CaseContent(BaseModel):
    """CaseResult 的可比较内容投影:只保留跨执行可重现的判定内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CaseStatus
    included_in_quality_score: bool
    fingerprint: str
    error: str | None
    event_types: tuple[str, ...]
    tool_calls: tuple[ToolCallContent, ...]
    operations: tuple[OperationContent, ...]
    final_state: FinalState | None


def case_content(result: CaseResult) -> CaseContent:
    """投影单个结果为可比较内容(剔除 Run/Workspace 标识与参数值)。"""
    return CaseContent(
        status=result.status,
        included_in_quality_score=result.included_in_quality_score,
        fingerprint=result.fingerprint,
        error=result.error,
        event_types=tuple(event.event_type for event in result.events),
        tool_calls=tuple(
            ToolCallContent(
                stable_key=call.stable_key,
                ordinal=call.ordinal,
                status=call.status,
                failure_code=call.failure_code,
            )
            for call in result.tool_calls
        ),
        operations=tuple(
            sorted(
                (
                    OperationContent(
                        status=operation.status, adapter_key=operation.adapter_key
                    )
                    for operation in result.operations
                ),
                key=lambda operation: (operation.status, operation.adapter_key),
            )
        ),
        final_state=result.final_state,
    )


def by_case_id(round_result: EvalRoundResult) -> dict[str, tuple[CaseContent, ...]]:
    """结果按 case_id 键控(重复按执行序),内容为可比较投影。

    不同执行顺序(如正序与倒序)对同一组 case 产生的轮次,经本函数投影后
    相等——即用例顺序无关性(计划原文测试语义)。
    """
    grouped: dict[str, list[CaseContent]] = {}
    for result in round_result.results:
        grouped.setdefault(result.case_id, []).append(case_content(result))
    return {case_id: tuple(contents) for case_id, contents in grouped.items()}


__all__ = [
    "CaseContent",
    "CaseResult",
    "CaseStatus",
    "EvalRoundResult",
    "EventCapture",
    "FinalState",
    "MessageCapture",
    "OperationCapture",
    "OperationContent",
    "ToolCallCapture",
    "ToolCallContent",
    "by_case_id",
    "case_content",
]
