"""测试 Anthropic 后端 — capabilities、chat 错误处理与 thinking 参数翻译。"""

import json

import httpx
import pytest
from anthropic import AsyncAnthropic

from agent_backend.protocol import BackendConfig, BackendProvider
from agent_backend.anthropic_backend import AnthropicAgent


def test_anthropic_capabilities():
    config = BackendConfig(
        provider=BackendProvider.ANTHROPIC,
        api_key="test-key",
    )
    backend = AnthropicAgent(config)
    caps = backend.capabilities()
    assert caps.supports_tool_calling
    assert caps.supports_thinking_level
    assert not caps.supports_json_mode
    assert caps.context_window == 200000


def test_anthropic_get_config():
    config = BackendConfig(
        provider=BackendProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        api_key="test-key",
    )
    backend = AnthropicAgent(config)
    retrieved = backend.get_config()
    assert retrieved.model == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_anthropic_chat_error_handling():
    config = BackendConfig(
        provider=BackendProvider.ANTHROPIC,
        api_key="invalid-key",
        api_base="https://invalid-anthropic-endpoint.example.com",
    )
    backend = AnthropicAgent(config)
    messages = [{"role": "user", "content": "Hello"}]
    events = []
    async for event in backend.chat(messages, [], "sess-001"):
        events.append(event)
    assert len(events) > 0
    assert events[-1].type == "typed_error"


# --- thinking 参数翻译（统一 thinking_level → Anthropic thinking） ---

_ANTHROPIC_SAMPLE_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
    '"role":"assistant","model":"claude","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
    '"usage":{"output_tokens":2}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


def _make_anthropic_backend(config, handler) -> AnthropicAgent:
    """替换真实客户端为 MockTransport 注入的 AsyncAnthropic，捕获请求体。"""
    backend = AnthropicAgent(config)
    backend._client = AsyncAnthropic(
        api_key=config.api_key,
        base_url=config.api_base or "https://api.anthropic.com",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return backend


def _anthropic_sse_response() -> httpx.Response:
    return httpx.Response(
        200,
        content=_ANTHROPIC_SAMPLE_STREAM,
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.asyncio
async def test_anthropic_chat_sends_thinking_when_configured():
    """配置 thinking_level 后请求体应带 thinking 参数且 temperature 强制为 1。"""
    captured: list[dict] = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return _anthropic_sse_response()

    config = BackendConfig(
        provider=BackendProvider.ANTHROPIC,
        api_key="test-key",
        api_base="https://api.anthropic.com",
        thinking_level="high",
    )
    backend = _make_anthropic_backend(config, handler)
    events = [e async for e in backend.chat([{"role": "user", "content": "Hi"}], [], "s1")]

    assert captured, "应捕获到 Anthropic 请求体"
    body = captured[0]
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert body["temperature"] == 1
    assert any(e.type == "text_delta" for e in events)


@pytest.mark.asyncio
async def test_anthropic_chat_no_thinking_by_default():
    """未配置 thinking_level 时请求体不应出现 thinking，temperature 保持配置值。"""
    captured: list[dict] = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return _anthropic_sse_response()

    config = BackendConfig(
        provider=BackendProvider.ANTHROPIC,
        api_key="test-key",
        api_base="https://api.anthropic.com",
    )
    backend = _make_anthropic_backend(config, handler)
    [e async for e in backend.chat([{"role": "user", "content": "Hi"}], [], "s1")]

    body = captured[0]
    assert "thinking" not in body
    assert body["temperature"] == 0.7