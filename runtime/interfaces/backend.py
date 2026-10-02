"""AgentBackend 统一接口(架构文档 §5)。

本模块只给出 Protocol 骨架与最小信封类型,不做任何实现:

- BackendEvent 的六种种类的白名单来自架构文档 §5;各 kind 的载荷字段由
  Harness 阶段定义,此处不提前发明。
- BackendRequest 的内容范围由架构文档 §5 约定(标准化 Message、固定版本、
  可见 ToolDefinition、可选 Structured Output Schema、生成参数与追踪上下文),
  具体字段由 ContextAssembler 阶段补齐。
- CancellationToken 从 Run 传入 Backend 与 ToolHandler,取消只在安全点生效
  (架构文档 §12);信号机制由 Harness 阶段实现。
"""

from collections.abc import AsyncIterator
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class BackendEventKind(str, Enum):
    """标准化 BackendEvent 只允许的种类(架构文档 §5 白名单)。"""

    TEXT_DELTA = "TextDelta"
    TOOL_CALL_DELTA = "ToolCallDelta"
    TOOL_CALL_COMPLETED = "ToolCallCompleted"
    USAGE_REPORTED = "UsageReported"
    BACKEND_COMPLETED = "BackendCompleted"
    BACKEND_FAILED = "BackendFailed"


class BackendEvent(BaseModel):
    """标准化 BackendEvent 信封;各 kind 的载荷字段由 Harness 阶段定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BackendEventKind


class BackendRequest(BaseModel):
    """AgentBackend 的请求载荷(架构文档 §5);字段由 ContextAssembler 阶段补齐。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentBackendCapabilities(BaseModel):
    """Backend 能力声明:能力以稳定 Key 表达(架构文档 §3.1、§4 启动校验)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_keys: frozenset[str] = frozenset()


class CancellationToken(BaseModel):
    """从 Run 传入 Backend 与支持取消的 ToolHandler 的取消令牌(架构文档 §12)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


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
        """流式生成标准化 BackendEvent。

        ToolCall 只有收到 ToolCallCompleted 且参数 JSON 可解析后才能进入
        Runtime;未完成的 ToolCallDelta 永远不能触发 Tool。
        """
        ...
