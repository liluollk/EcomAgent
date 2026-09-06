"""
AgentEvent 流式状态机 — 八类事件的可辨识联合类型。

使用统一事件模型（可辨识联合类型），以 Literal 字段区分事件类型，
实现类型安全的事件分发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union


@dataclass
class TypedError:
    """结构化错误，包含错误码、标题、描述、是否可重试。

    用于向调用方传递可操作的错误信息，
    而非原始异常堆栈。
    """

    code: str
    title: str
    message: str
    can_retry: bool = False


@dataclass
class TextDeltaEvent:
    """LLM 流式输出的文本增量片段。

    每次 LLM 产生新的文本 token 时，后端通过 AsyncGenerator 发送此事件。
    调用方累积所有 text_delta 事件的 text 字段即可得到完整回复。
    """

    type: Literal["text_delta"] = "text_delta"
    text: str = ""


@dataclass
class ToolStartEvent:
    """LLM 决定调用工具时发送的事件。

    包含工具名称、调用 ID（用于匹配 tool_result）和参数。
    调用方收到此事件后应执行对应工具，然后发送 ToolResultEvent。

    可观测性字段（引擎回填，缺省不破坏既有消费方）：
    trace_id  一次 turn 的执行轨迹 ID（Trace 从事件流上自然生长）
    source    工具来源通道：commerce（内置电商工具）/ mcp / ""（内部）
    """

    type: Literal["tool_start"] = "tool_start"
    tool_name: str = ""
    tool_use_id: str = ""
    input: dict[str, Any] = None
    trace_id: str = ""
    source: str = ""

    def __post_init__(self):
        if self.input is None:
            self.input = {}


@dataclass
class ToolResultEvent:
    """工具执行完成后返回的结果事件。

    tool_use_id 必须与对应的 ToolStartEvent 一致，以便调用方匹配。
    is_error 为 True 时表示工具执行失败。

    可观测性字段（Execution Policy 回填，缺省不破坏既有消费方）：
    attempt            实际执行尝试次数（重试后 > 1，Trace 可还原重试链）
    duration_ms        工具执行总耗时（含重试与退避等待）
    idempotent_replay  命中幂等回放（同键重复请求未重复产生副作用）
    trace_id / source  与配对 ToolStartEvent 同值
    """

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = ""
    tool_name: str = ""
    result: str = ""
    is_error: bool = False
    attempt: int = 1
    duration_ms: int = 0
    idempotent_replay: bool = False
    trace_id: str = ""
    source: str = ""


@dataclass
class PermissionRequestEvent:
    """权限请求事件 — 当工具调用需要用户确认时发送。

    包含请求 ID、工具名、参数和原因。
    调用方需响应该请求（allow/deny），随后 Agent 才能继续执行。
    """

    type: Literal["permission_request"] = "permission_request"
    request_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = None
    reason: str = ""

    def __post_init__(self):
        if self.tool_input is None:
            self.tool_input = {}


@dataclass
class TypedErrorEvent:
    """结构化错误事件 — 当发生可恢复或不可恢复的错误时发送。

    与 TextDeltaEvent 不同，此事件携带 TypedError 结构体，
    包含错误码和可操作信息，而非纯文本描述。
    """

    type: Literal["typed_error"] = "typed_error"
    error: Optional[TypedError] = None


@dataclass
class StatusEvent:
    """状态事件 — 用于向调用方报告 Agent 当前执行阶段。

    用于 UI 进度提示。
    例如：正在解析技能、正在检查前置条件、正在构建提示词等。
    """

    type: Literal["status"] = "status"
    message: str = ""


@dataclass
class CompleteEvent:
    """完成事件 — 表示当前 turn 正常结束。

    标记一个完整的请求-响应周期结束。
    """

    type: Literal["complete"] = "complete"


@dataclass
class AbortEvent:
    """中断事件 — 表示当前 turn 被用户或系统中断。

    标记一个未完成的请求-响应周期被中断。
    调用方收到此事件后应停止渲染并结束当前 turn。
    """

    type: Literal["abort"] = "abort"
    reason: str = ""


# AgentEvent 可辨识联合类型 — 八类事件
# 使用 Union 而非继承，因为 Pydantic 的 discriminated union 依赖 Literal 字段
AgentEvent = Union[
    TextDeltaEvent,
    ToolStartEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    TypedErrorEvent,
    StatusEvent,
    CompleteEvent,
    AbortEvent,
]