"""Provider 流事件 → 标准化 BackendEvent 的归一化测试(纯转换,不发网络请求)。"""

import json
from types import SimpleNamespace

import pytest

from adapters.model_backends.anthropic_backend import (
    AnthropicBackend,
    AnthropicStreamAccumulator,
)
from adapters.model_backends.openai_backend import (
    OpenAIBackend,
    OpenAIStreamAccumulator,
    normalize_status_code,
)
from runtime.backend.protocol import (
    BackendCompleted,
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
from runtime.capabilities.skill_activation import LOAD_SKILL_DEFINITION


def openai_chunk(**kwargs: object) -> dict:
    return kwargs


async def test_openai_text_deltas_become_text_delta_events() -> None:
    accumulator = OpenAIStreamAccumulator()
    events = accumulator.process(
        openai_chunk(choices=[{"delta": {"content": "你"}}])
    ) + accumulator.process(openai_chunk(choices=[{"delta": {"content": "好"}}]))
    texts = [event.text_delta for event in events if isinstance(event, TextDelta)]
    assert texts == ["你", "好"]


async def test_openai_tool_call_arguments_accumulate_and_complete() -> None:
    accumulator = OpenAIStreamAccumulator()
    events: list = []
    events += accumulator.process(
        openai_chunk(
            choices=[
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "lookup", "arguments": '{"sku": "A'},
                            }
                        ]
                    }
                }
            ]
        )
    )
    events += accumulator.process(
        openai_chunk(choices=[{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '-1"}'}}]}}])
    )
    events += accumulator.process(openai_chunk(choices=[{"delta": {}, "finish_reason": "tool_calls"}]))
    events += accumulator.finish()
    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    completed = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert deltas, "增量片段应发出 ToolCallDelta"
    assert len(completed) == 1
    assert completed[0].call_id == "call-1"
    assert completed[0].name == "lookup"
    assert completed[0].arguments == {"sku": "A-1"}


async def test_openai_unparseable_arguments_yield_backend_failed() -> None:
    accumulator = OpenAIStreamAccumulator()
    accumulator.process(
        openai_chunk(
            choices=[
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call-2", "function": {"name": "update_price", "arguments": "{"}}
                        ]
                    }
                }
            ]
        )
    )
    accumulator.process(openai_chunk(choices=[{"delta": {}, "finish_reason": "tool_calls"}]))
    events = accumulator.finish()
    assert not [event for event in events if isinstance(event, ToolCallCompleted)]
    failures = [event for event in events if isinstance(event, BackendFailed)]
    assert len(failures) == 1
    assert failures[0].error_code == "INVALID_TOOL_CALL_ARGUMENTS"
    assert failures[0].retryable is False


async def test_openai_usage_chunk_reports_normalized_usage() -> None:
    accumulator = OpenAIStreamAccumulator()
    events = accumulator.process(
        openai_chunk(
            choices=[],
            usage={"prompt_tokens": 12, "completion_tokens": 34},
        )
    )
    usage = [event for event in events if isinstance(event, UsageReported)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 12
    assert usage[0].output_tokens == 34


async def test_openai_stream_ends_with_backend_completed() -> None:
    accumulator = OpenAIStreamAccumulator()
    accumulator.process(openai_chunk(choices=[{"delta": {"content": "ok"}}]))
    accumulator.process(openai_chunk(choices=[{"delta": {}, "finish_reason": "stop"}]))
    events = accumulator.finish()
    assert isinstance(events[-1], BackendCompleted)
    assert events[-1].finish_reason == "stop"


async def test_openai_backend_sends_json_serializable_tool_definitions() -> None:
    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Completions:
        async def create(self, **kwargs):
            json.dumps(kwargs["tools"])
            assert kwargs["tools"] == [
                {
                    "type": "function",
                    "function": {
                        "name": "load_skill",
                        "description": LOAD_SKILL_DEFINITION.description,
                        "parameters": LOAD_SKILL_DEFINITION.input_json_schema,
                    },
                }
            ]
            return EmptyStream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    backend = OpenAIBackend(client, "deepseek-flash")
    request = BackendRequest(messages=(), tool_definitions=(LOAD_SKILL_DEFINITION,))

    events = [event async for event in backend.stream(request, CancellationToken())]

    assert isinstance(events[-1], BackendCompleted)


async def test_openai_backend_serializes_assistant_tool_call_and_tool_result_pair() -> None:
    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Completions:
        async def create(self, **kwargs):
            assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
            assert kwargs["messages"] == [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-123",
                            "type": "function",
                            "function": {
                                "name": "search_products",
                                "arguments": '{"query":"coffee"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": '{"items":[]}',
                    "tool_call_id": "call-123",
                    "name": "search_products",
                },
            ]
            return EmptyStream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    backend = OpenAIBackend(
        client,
        "deepseek-flash",
        extra_body={"thinking": {"type": "disabled"}},
    )
    request = BackendRequest(
        messages=(
            BackendMessage(
                role="assistant",
                content="",
                tool_calls=(
                    BackendToolCall(
                        id="call-123",
                        name="search_products",
                        arguments={"query": "coffee"},
                    ),
                ),
            ),
            BackendMessage(
                role="tool",
                content='{"items":[]}',
                name="search_products",
                call_id="call-123",
            ),
        )
    )

    events = [event async for event in backend.stream(request, CancellationToken())]

    assert isinstance(events[-1], BackendCompleted)


async def test_anthropic_backend_sends_json_serializable_tool_definitions() -> None:
    class EmptyStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Messages:
        def stream(self, **kwargs):
            json.dumps(kwargs["tools"])
            return EmptyStream()

    client = SimpleNamespace(messages=Messages())
    backend = AnthropicBackend(client, "claude-test")
    request = BackendRequest(messages=(), tool_definitions=(LOAD_SKILL_DEFINITION,))

    events = [
        event
        async for event in backend.stream(request, CancellationToken())
    ]

    assert isinstance(events[-1], BackendCompleted)


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_retryable"),
    [
        (400, "INVALID_REQUEST", False),
        (401, "AUTH_FAILED", False),
        (403, "AUTH_FAILED", False),
        (408, "TIMEOUT", True),
        (429, "RATE_LIMITED", True),
        (500, "PROVIDER_UNAVAILABLE", True),
        (503, "PROVIDER_UNAVAILABLE", True),
    ],
)
def test_openai_status_codes_normalize_deterministically(
    status: int, expected_code: str, expected_retryable: bool
) -> None:
    error_code, retryable = normalize_status_code(status)
    assert error_code == expected_code
    assert retryable == expected_retryable


async def test_anthropic_text_and_tool_use_stream_normalizes() -> None:
    accumulator = AnthropicStreamAccumulator()
    events: list = []
    events += accumulator.process({"type": "message_start", "message": {"usage": {"input_tokens": 21}}})
    events += accumulator.process(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
    )
    events += accumulator.process(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "查"}}
    )
    events += accumulator.process(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu-1", "name": "lookup"},
        }
    )
    events += accumulator.process(
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"sku":'}}
    )
    events += accumulator.process(
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "A-1"}'}}
    )
    events += accumulator.process({"type": "content_block_stop", "index": 1})
    events += accumulator.process(
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}}
    )
    events += accumulator.process({"type": "message_stop"})
    events += accumulator.finish()

    texts = [event.text_delta for event in events if isinstance(event, TextDelta)]
    assert texts == ["查"]
    completed = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert len(completed) == 1
    assert completed[0].call_id == "toolu-1"
    assert completed[0].name == "lookup"
    assert completed[0].arguments == {"sku": "A-1"}
    usage = [event for event in events if isinstance(event, UsageReported)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 21
    assert usage[0].output_tokens == 40
    finished = [event for event in events if isinstance(event, BackendCompleted)]
    assert finished[-1].finish_reason == "tool_calls"


async def test_anthropic_empty_tool_input_completes_with_empty_arguments() -> None:
    accumulator = AnthropicStreamAccumulator()
    accumulator.process(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu-2", "name": "list_products"},
        }
    )
    accumulator.process({"type": "content_block_stop", "index": 0})
    accumulator.process({"type": "message_stop"})
    events = accumulator.finish()
    completed = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert len(completed) == 1
    assert completed[0].arguments == {}


async def test_anthropic_unparseable_tool_arguments_fail_closed() -> None:
    accumulator = AnthropicStreamAccumulator()
    accumulator.process(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu-3", "name": "update_price"},
        }
    )
    accumulator.process(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{"}}
    )
    accumulator.process({"type": "message_stop"})
    events = accumulator.finish()
    assert not [event for event in events if isinstance(event, ToolCallCompleted)]
    failures = [event for event in events if isinstance(event, BackendFailed)]
    assert len(failures) == 1
    assert failures[0].error_code == "INVALID_TOOL_CALL_ARGUMENTS"


async def test_anthropic_error_event_becomes_backend_failed() -> None:
    accumulator = AnthropicStreamAccumulator()
    events = accumulator.process(
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    )
    failures = [event for event in events if isinstance(event, BackendFailed)]
    assert len(failures) == 1
    assert failures[0].retryable is True
