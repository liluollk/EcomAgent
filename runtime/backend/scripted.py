"""ScriptedBackend:按给定事件序列回放的确定性 AgentBackend。

用于 Runtime 测试与故障注入;不发起任何网络请求。
"""

from collections.abc import AsyncIterator, Sequence

from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)


class ScriptedBackend:
    """按脚本顺序回放事件;在事件边界检查取消信号。"""

    def __init__(
        self,
        events: Sequence[BackendEvent],
        capability_keys: frozenset[str] | None = None,
    ) -> None:
        self.stream_script: tuple[BackendEvent, ...] = tuple(events)
        self._capabilities = AgentBackendCapabilities(
            capability_keys=capability_keys
            if capability_keys is not None
            else frozenset({"streaming", "tool_calling"})
        )

    def capabilities(self) -> AgentBackendCapabilities:
        return self._capabilities

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        for event in self.stream_script:
            if cancellation.is_cancelled:
                return
            yield event
