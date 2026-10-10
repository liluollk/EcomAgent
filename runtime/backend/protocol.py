"""AgentBackend 统一协议与标准化事件信封(架构文档 §5)。

本模块是 Backend 协议的权威定义;runtime.interfaces.backend 从这里重导出,
保持既有 import 路径兼容。

- 标准化 BackendEvent 只允许六种(白名单),每种以 BackendEvent 子类钉住 kind,
  载荷字段在子类中设为必填,防止半构造事件进入 Runtime。
- ToolCall 只有收到 ToolCallCompleted 且参数 JSON 可解析后才进入 Runtime;
  未完成的 ToolCallDelta 永远不能触发 Tool。
- CancellationToken 是协作式取消令牌,取消只在安全点生效(架构文档 §12)。
"""

from collections.abc import AsyncIterator, Mapping
from enum import Enum
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator

from runtime.interfaces.tools import ToolDefinition


class BackendEventKind(str, Enum):
    """标准化 BackendEvent 只允许的种类(架构文档 §5 白名单)。"""

    TEXT_DELTA = "TextDelta"
    TOOL_CALL_DELTA = "ToolCallDelta"
    TOOL_CALL_COMPLETED = "ToolCallCompleted"
    USAGE_REPORTED = "UsageReported"
    BACKEND_COMPLETED = "BackendCompleted"
    BACKEND_FAILED = "BackendFailed"


class BackendEvent(BaseModel):
    """标准化 BackendEvent 信封。

    扁平载荷 + kind 白名单校验;各事件子类把 kind 钉为 Literal 并把载荷
    字段设为必填,因此只有完整事件才能被构造出来。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BackendEventKind
    text_delta: str | None = None
    call_id: str | None = None
    index: int | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    arguments_delta: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self) -> Self:
        required: dict[BackendEventKind, tuple[str, ...]] = {
            BackendEventKind.TEXT_DELTA: ("text_delta",),
            BackendEventKind.TOOL_CALL_DELTA: ("arguments_delta",),
            BackendEventKind.TOOL_CALL_COMPLETED: ("call_id", "name", "arguments"),
            BackendEventKind.USAGE_REPORTED: ("input_tokens", "output_tokens"),
            BackendEventKind.BACKEND_COMPLETED: ("finish_reason",),
            BackendEventKind.BACKEND_FAILED: ("error_code", "retryable"),
        }
        missing = [
            field for field in required[self.kind] if getattr(self, field) is None
        ]
        if missing:
            raise ValueError(f"{self.kind.value} requires fields: {', '.join(missing)}")
        return self


class TextDelta(BackendEvent):
    """增量文本片段。"""

    kind: Literal[BackendEventKind.TEXT_DELTA] = BackendEventKind.TEXT_DELTA
    text_delta: str


class ToolCallDelta(BackendEvent):
    """ToolCall 参数增量;永远不能单独触发 Tool。"""

    kind: Literal[BackendEventKind.TOOL_CALL_DELTA] = BackendEventKind.TOOL_CALL_DELTA
    arguments_delta: str


class ToolCallCompleted(BackendEvent):
    """完整 ToolCall;参数 JSON 已可解析。"""

    kind: Literal[BackendEventKind.TOOL_CALL_COMPLETED] = (
        BackendEventKind.TOOL_CALL_COMPLETED
    )
    call_id: str
    name: str
    arguments: dict[str, Any]


class UsageReported(BackendEvent):
    """Provider 用量归一化结果。"""

    kind: Literal[BackendEventKind.USAGE_REPORTED] = BackendEventKind.USAGE_REPORTED
    input_tokens: int
    output_tokens: int


class BackendCompleted(BackendEvent):
    """模型流正常完成。"""

    kind: Literal[BackendEventKind.BACKEND_COMPLETED] = (
        BackendEventKind.BACKEND_COMPLETED
    )
    finish_reason: str


class BackendFailed(BackendEvent):
    """Provider 失败,已归一化为确定性错误码;error_message 不含响应体。"""

    kind: Literal[BackendEventKind.BACKEND_FAILED] = BackendEventKind.BACKEND_FAILED
    error_code: str
    retryable: bool


class BackendToolCall(BaseModel):
    """Provider 无关的已完成函数调用历史,用于重建合法 Assistant 消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]


class BackendMessage(BaseModel):
    """进入 BackendRequest 的标准化 Message。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    content: str = ""
    name: str | None = None
    call_id: str | None = None
    tool_calls: tuple[BackendToolCall, ...] = ()


class BackendRequest(BaseModel):
    """AgentBackend 的请求载荷(架构文档 §5)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[BackendMessage, ...]
    tool_definitions: tuple[ToolDefinition, ...] = ()
    model_key: str | None = None
    model_parameters: Mapping[str, Any] = {}
    structured_output_schema: Mapping[str, Any] | None = None
    tracing: Mapping[str, str] = {}


class AgentBackendCapabilities(BaseModel):
    """Backend 能力声明:能力以稳定 Key 表达(架构文档 §3.1、§4)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_keys: frozenset[str] = frozenset()


class CancellationToken:
    """协作式取消令牌;取消只在 Backend / ToolHandler 的安全点被检查。"""

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


class AgentBackend(Protocol):
    """统一模型后端协议(架构文档 §5)。

    Runtime 只依赖该协议,不根据厂商名称写特殊分支;OpenAI 兼容服务
    (如 DeepSeek)通过 OpenAIBackend 使用自定义 Base URL 接入。
    """

    def capabilities(self) -> AgentBackendCapabilities:
        """声明当前 Backend 支持的稳定能力 Key,供启动校验比对。"""
        ...

    def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        """流式生成标准化 BackendEvent。"""
        ...


__all__ = [
    "AgentBackend",
    "AgentBackendCapabilities",
    "BackendCompleted",
    "BackendEvent",
    "BackendEventKind",
    "BackendFailed",
    "BackendMessage",
    "BackendRequest",
    "BackendToolCall",
    "CancellationToken",
    "TextDelta",
    "ToolCallCompleted",
    "ToolCallDelta",
    "UsageReported",
]
