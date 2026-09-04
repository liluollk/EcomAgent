"""测试 OpenAI 后端 — capabilities、chat 错误处理与 reasoning_effort 翻译。"""

import json

import httpx
import pytest
from openai import AsyncOpenAI

from agent_backend.protocol import BackendConfig, BackendProvider
from agent_backend.openai_backend import OpenAIAgent


def test_openai_capabilities():
    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        api_key="test-key",
    )
    backend = OpenAIAgent(config)
    caps = backend.capabilities()
    assert caps.supports_tool_calling
    assert caps.supports_json_mode
    assert not caps.supports_thinking_level


def test_openai_get_config():
    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        model="gpt-4o",
        api_key="test-key",
    )
    backend = OpenAIAgent(config)
    retrieved = backend.get_config()
    assert retrieved.model == "gpt-4o"
    assert retrieved.api_key == "test-key"


@pytest.mark.asyncio
async def test_openai_chat_error_handling():
    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        api_key="invalid-key",
        api_base="https://invalid-openai-endpoint.example.com",
    )
    backend = OpenAIAgent(config)
    messages = [{"role": "user", "content": "Hello"}]
    events = []
    async for event in backend.chat(messages, [], "sess-001"):
        events.append(event)
    assert len(events) > 0
    assert events[-1].type == "typed_error"


# --- capabilities 模型名启发式 ---


def test_openai_capabilities_thinking_model():
    """推理模型（o 系列）应声明支持 thinking。"""
    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        model="o3-mini",
        api_key="test-key",
    )
    assert OpenAIAgent(config).capabilities().supports_thinking_level


def test_openai_capabilities_non_thinking_model():
    """非推理模型（gpt-4o 系列）不应声明支持 thinking。"""
    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        model="gpt-4o-mini",
        api_key="test-key",
    )
    assert not OpenAIAgent(config).capabilities().supports_thinking_level


# --- reasoning_effort 参数翻译（统一 thinking_level → OpenAI reasoning_effort） ---

_OPENAI_SAMPLE_STREAM = (
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,'
    '"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant",'
    '"content":"hi"},"finish_reason":null}]}\n\n'
    'data: [DONE]\n\n'
)


def _make_openai_backend(config, handler) -> OpenAIAgent:
    """替换真实客户端为 MockTransport 注入的 AsyncOpenAI，捕获请求体。"""
    backend = OpenAIAgent(config)
    backend._client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.api_base or "https://api.openai.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return backend


def _openai_sse_response() -> httpx.Response:
    return httpx.Response(
        200,
        content=_OPENAI_SAMPLE_STREAM,
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.asyncio
async def test_openai_chat_sends_reasoning_effort_when_configured():
    """配置 thinking_level 后请求体应带 reasoning_effort 且其值原样透传。"""
    captured: list[dict] = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return _openai_sse_response()

    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        model="o3-mini",
        api_key="test-key",
        api_base="https://api.openai.com/v1",
        thinking_level="high",
    )
    backend = _make_openai_backend(config, handler)
    events = [e async for e in backend.chat([{"role": "user", "content": "Hi"}], [], "s1")]

    assert captured, "应捕获到 OpenAI 请求体"
    body = captured[0]
    assert body.get("reasoning_effort") == "high"
    assert any(e.type == "text_delta" for e in events)


@pytest.mark.asyncio
async def test_openai_chat_no_reasoning_effort_by_default():
    """未配置 thinking_level 时请求体不应出现 reasoning_effort。"""
    captured: list[dict] = []

    async def handler(request):
        captured.append(json.loads(request.content))
        return _openai_sse_response()

    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        model="gpt-4o-mini",
        api_key="test-key",
        api_base="https://api.openai.com/v1",
    )
    backend = _make_openai_backend(config, handler)
    [e async for e in backend.chat([{"role": "user", "content": "Hi"}], [], "s1")]

    body = captured[0]
    assert "reasoning_effort" not in body