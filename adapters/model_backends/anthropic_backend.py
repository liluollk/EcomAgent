"""Anthropic Backend。

只负责 SDK 调用与流协议适配:流事件 → 标准化 BackendEvent 的归一化、
Tool Calling 格式归一化、Provider 错误映射、Usage 归一化、取消能力。
归一化逻辑与 SDK 类型解耦(输入为 dict),可以在不发网络请求的情况下测试。
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendFailed,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    UsageReported,
)


ANTHROPIC_ERROR_CODES: dict[str, tuple[str, bool]] = {
    "invalid_request_error": ("INVALID_REQUEST", False),
    "authentication_error": ("AUTH_FAILED", False),
    "permission_error": ("AUTH_FAILED", False),
    "not_found_error": ("INVALID_REQUEST", False),
    "request_too_large": ("INVALID_REQUEST", False),
    "rate_limit_error": ("RATE_LIMITED", True),
    "api_error": ("PROVIDER_UNAVAILABLE", True),
    "overloaded_error": ("PROVIDER_OVERLOADED", True),
    "timeout_error": ("TIMEOUT", True),
}


def normalize_anthropic_error_type(error_type: str) -> tuple[str, bool]:
    """把 Anthropic 错误事件类型映射为 (error_code, retryable);映射是确定性的。"""
    return ANTHROPIC_ERROR_CODES.get(error_type, ("PROVIDER_ERROR", False))


def map_stop_reason(stop_reason: str | None) -> str:
    """把 Anthropic stop_reason 映射为与 OpenAI 归一化一致的 finish_reason。"""
    if stop_reason == "tool_use":
        return "tool_calls"
    return stop_reason or "stop"


class _PendingToolCall:
    __slots__ = ("arguments", "call_id", "name")

    def __init__(self, call_id: str | None, name: str | None) -> None:
        self.call_id = call_id
        self.name = name
        self.arguments = ""

    def is_complete(self) -> bool:
        return self.call_id is not None and self.name is not None


class AnthropicStreamAccumulator:
    """增量累积 messages 流事件;finish() 时发出终态事件。

    ToolCallCompleted 只在参数 JSON 完整可解析后发出;不可解析的参数
    以确定性 BackendFailed 收尾,绝不让半构造 ToolCall 进入 Runtime。
    """

    def __init__(self) -> None:
        self._pending: dict[int, _PendingToolCall] = {}
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._stop_reason: str | None = None
        self._failed = False

    def process(self, event: dict[str, Any]) -> list[BackendEvent]:
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            self._input_tokens = int(usage.get("input_tokens") or 0)
            return []
        if event_type == "content_block_start":
            return self._process_block_start(event)
        if event_type == "content_block_delta":
            return self._process_block_delta(event)
        if event_type == "message_delta":
            return self._process_message_delta(event)
        if event_type == "error":
            return self._process_error(event)
        return []

    def _process_block_start(self, event: dict[str, Any]) -> list[BackendEvent]:
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            index = int(event.get("index") or 0)
            self._pending[index] = _PendingToolCall(block.get("id"), block.get("name"))
        return []

    def _process_block_delta(self, event: dict[str, Any]) -> list[BackendEvent]:
        index = int(event.get("index") or 0)
        delta = event.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            return [TextDelta(text_delta=delta.get("text") or "")]
        if delta_type == "input_json_delta":
            pending = self._pending.setdefault(index, _PendingToolCall(None, None))
            piece = delta.get("partial_json") or ""
            pending.arguments += piece
            return [
                ToolCallDelta(
                    call_id=pending.call_id,
                    index=index,
                    arguments_delta=piece,
                )
            ]
        return []

    def _process_message_delta(self, event: dict[str, Any]) -> list[BackendEvent]:
        delta = event.get("delta") or {}
        stop_reason = delta.get("stop_reason")
        if stop_reason:
            self._stop_reason = stop_reason
        usage = event.get("usage") or {}
        if usage.get("output_tokens") is not None:
            self._output_tokens = int(usage["output_tokens"])
        return []

    def _process_error(self, event: dict[str, Any]) -> list[BackendEvent]:
        error = event.get("error") or {}
        error_type = error.get("type") or "provider_error"
        error_code, retryable = normalize_anthropic_error_type(error_type)
        self._failed = True
        return [
            BackendFailed(
                error_code=error_code,
                error_message=error_type,
                retryable=retryable,
            )
        ]

    def finish(self) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        if self._input_tokens is not None or self._output_tokens is not None:
            events.append(
                UsageReported(
                    input_tokens=self._input_tokens or 0,
                    output_tokens=self._output_tokens or 0,
                )
            )
        if not self._failed:
            for index in sorted(self._pending):
                completed = self._complete_tool_call(self._pending[index])
                if completed is not None:
                    events.append(completed)
        events.append(BackendCompleted(finish_reason=map_stop_reason(self._stop_reason)))
        return events

    def _complete_tool_call(self, pending: _PendingToolCall) -> BackendEvent | None:
        if not pending.is_complete():
            return None
        try:
            arguments = json.loads(pending.arguments) if pending.arguments else {}
        except json.JSONDecodeError:
            return BackendFailed(
                error_code="INVALID_TOOL_CALL_ARGUMENTS",
                error_message="tool call arguments are not valid JSON",
                retryable=False,
            )
        if not isinstance(arguments, dict):
            return BackendFailed(
                error_code="INVALID_TOOL_CALL_ARGUMENTS",
                error_message="tool call arguments must be a JSON object",
                retryable=False,
            )
        return ToolCallCompleted(
            call_id=pending.call_id or "",
            name=pending.name or "",
            arguments=arguments,
        )


class AnthropicBackend:
    """Anthropic Messages API 后端。"""

    def __init__(
        self,
        client: Any,
        model: str,
        extra_capability_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._client = client
        self._model = model
        self._capabilities = AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"}) | extra_capability_keys
        )

    def capabilities(self) -> AgentBackendCapabilities:
        return self._capabilities

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        accumulator = AnthropicStreamAccumulator()
        try:
            async with self._client.messages.stream(
                model=self._model,
                messages=[
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                tools=[tool.model_dump() for tool in request.tool_definitions] or None,
            ) as stream:
                async for event in stream:
                    if cancellation.is_cancelled:
                        break
                    for normalized in accumulator.process(event.model_dump()):
                        yield normalized
        except Exception as exc:  # noqa: BLE001 - 归一化边界必须兜住所有 Provider 异常
            error_type = getattr(exc, "type", None)
            if isinstance(error_type, str):
                error_code, retryable = normalize_anthropic_error_type(error_type)
            else:
                error_code, retryable = "PROVIDER_CONNECTION_ERROR", True
            yield BackendFailed(
                error_code=error_code,
                error_message=type(exc).__name__,
                retryable=retryable,
            )
            return
        for event in accumulator.finish():
            yield event
