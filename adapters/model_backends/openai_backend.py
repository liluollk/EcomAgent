"""OpenAI(及 OpenAI 兼容服务,如 DeepSeek)Backend。

只负责 SDK 调用与流协议适配:流块 → 标准化 BackendEvent 的归一化、
Tool Calling 格式归一化、Provider 错误映射、Usage 归一化、取消能力。
归一化逻辑与 SDK 类型解耦(输入为 dict),因此可以在不发网络请求的情况下测试。
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import openai

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


def normalize_status_code(status_code: int) -> tuple[str, bool]:
    """把 Provider HTTP 状态码映射为 (error_code, retryable);映射是确定性的。"""
    if status_code in {400, 404, 405, 422}:
        return "INVALID_REQUEST", False
    if status_code in {401, 403}:
        return "AUTH_FAILED", False
    if status_code == 408:
        return "TIMEOUT", True
    if status_code == 429:
        return "RATE_LIMITED", True
    if status_code >= 500:
        return "PROVIDER_UNAVAILABLE", True
    return "PROVIDER_ERROR", False


def normalize_provider_exception(exc: Exception) -> BackendFailed:
    """把 SDK 异常归一化为 BackendFailed;消息只含异常类型名,不回显响应体。"""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        error_code, retryable = normalize_status_code(status_code)
    elif isinstance(exc, openai.APITimeoutError):
        error_code, retryable = "TIMEOUT", True
    else:
        error_code, retryable = "PROVIDER_CONNECTION_ERROR", True
    return BackendFailed(
        error_code=error_code,
        error_message=type(exc).__name__,
        retryable=retryable,
    )


class _PendingToolCall:
    __slots__ = ("arguments", "call_id", "name")

    def __init__(self, call_id: str | None, name: str | None) -> None:
        self.call_id = call_id
        self.name = name
        self.arguments = ""

    def is_complete(self) -> bool:
        return self.call_id is not None and self.name is not None


class OpenAIStreamAccumulator:
    """增量累积 chat.completions 流块;finish() 时发出终态事件。

    ToolCallCompleted 只在参数 JSON 完整可解析后发出;不可解析的参数
    以确定性 BackendFailed 收尾,绝不让半构造 ToolCall 进入 Runtime。
    """

    def __init__(self) -> None:
        self._pending: dict[int, _PendingToolCall] = {}
        self._finish_reason: str | None = None

    def process(self, chunk: dict[str, Any]) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            events.append(
                UsageReported(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                )
            )
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                events.append(TextDelta(text_delta=content))
            for tool_call in delta.get("tool_calls") or []:
                events.extend(self._process_tool_call_delta(tool_call))
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                self._finish_reason = finish_reason
        return events

    def _process_tool_call_delta(self, tool_call: dict[str, Any]) -> list[BackendEvent]:
        index = int(tool_call.get("index") or 0)
        function = tool_call.get("function") or {}
        pending = self._pending.setdefault(index, _PendingToolCall(None, None))
        call_id = tool_call.get("id")
        name = function.get("name")
        if call_id:
            pending.call_id = call_id
        if name:
            pending.name = name
        arguments_delta = function.get("arguments") or ""
        if not arguments_delta:
            return []
        pending.arguments += arguments_delta
        return [
            ToolCallDelta(
                call_id=pending.call_id,
                index=index,
                arguments_delta=arguments_delta,
            )
        ]

    def finish(self) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        for index in sorted(self._pending):
            pending = self._pending[index]
            completed = self._complete_tool_call(pending)
            if completed is not None:
                events.append(completed)
        events.append(BackendCompleted(finish_reason=self._finish_reason or "stop"))
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


class OpenAIBackend:
    """OpenAI / OpenAI 兼容后端;DeepSeek 等通过自定义 client 接入。

    Runtime 只依赖 AgentBackend 协议,不感知厂商名称。
    """

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
        accumulator = OpenAIStreamAccumulator()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[message.model_dump(exclude_none=True) for message in request.messages],
                tools=[tool.model_dump() for tool in request.tool_definitions] or None,
                stream=True,
            )
            async for chunk in response:
                if cancellation.is_cancelled:
                    break
                for event in accumulator.process(chunk.model_dump()):
                    yield event
        except (openai.APIStatusError, openai.APIConnectionError, openai.APITimeoutError) as exc:
            yield normalize_provider_exception(exc)
            return
        for event in accumulator.finish():
            yield event
