"""ScriptedBackend 与 AgentBackend 协议信封的不变式测试。"""

from runtime.backend.protocol import (
    BackendCompleted,
    BackendEvent,
    BackendFailed,
    BackendMessage,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)
from runtime.backend.scripted import ScriptedBackend


def make_request() -> BackendRequest:
    return BackendRequest(
        messages=(BackendMessage(role="user", content="查询商品价格"),),
    )


async def test_scripted_backend_emits_completed_tool_call() -> None:
    backend = ScriptedBackend([
        ToolCallCompleted(call_id="call-1", name="lookup", arguments={"sku": "A-1"}),
        BackendCompleted(finish_reason="tool_calls"),
    ])
    events = [event async for event in backend.stream(make_request(), CancellationToken())]
    assert events[0].name == "lookup"
    assert events[0].arguments == {"sku": "A-1"}


async def test_scripted_backend_replays_scripted_events_in_order() -> None:
    backend = ScriptedBackend([
        TextDelta(text_delta="查"),
        TextDelta(text_delta="询"),
        BackendCompleted(finish_reason="stop"),
    ])
    events = [event async for event in backend.stream(make_request(), CancellationToken())]
    assert [event.kind for event in events] == [
        event.kind for event in backend.stream_script
    ]
    assert events[0].text_delta == "查"
    assert events[1].text_delta == "询"
    assert events[-1].finish_reason == "stop"


async def test_scripted_backend_stops_after_cancellation() -> None:
    backend = ScriptedBackend([
        TextDelta(text_delta="a"),
        TextDelta(text_delta="b"),
        BackendCompleted(finish_reason="stop"),
    ])
    cancellation = CancellationToken()
    collected: list[BackendEvent] = []
    async for event in backend.stream(make_request(), cancellation):
        collected.append(event)
        cancellation.cancel()
    assert len(collected) == 1
    assert collected[0].text_delta == "a"


async def test_scripted_backend_reports_streaming_capability() -> None:
    backend = ScriptedBackend([])
    capabilities = backend.capabilities()
    assert "streaming" in capabilities.capability_keys
    assert "tool_calling" in capabilities.capability_keys


async def test_tool_call_completed_requires_parseable_shape() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ToolCallCompleted(call_id="call-1", name="lookup")


async def test_backend_failed_carries_error_classification() -> None:
    failure = BackendFailed(error_code="RATE_LIMITED", error_message="slow down", retryable=True)
    assert failure.retryable is True
    assert failure.kind.value == "BackendFailed"
