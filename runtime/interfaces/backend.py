"""AgentBackend 统一接口(架构文档 §5)。

权威定义在 runtime.backend.protocol;此处重导出以保持既有 import 路径稳定。
"""

from runtime.backend.protocol import (
    AgentBackend,
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendEventKind,
    BackendFailed,
    BackendMessage,
    BackendRequest,
    BackendToolCall,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    UsageReported,
)


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
