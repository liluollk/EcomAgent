"""测试后端工厂创建。"""

import pytest
from agent_backend.protocol import BackendConfig, BackendProvider, AgentCapabilities
from agent_backend.factory import create_backend
from agent_backend.openai_backend import OpenAIAgent
from agent_backend.anthropic_backend import AnthropicAgent


def test_create_openai_backend():
    config = BackendConfig(
        provider=BackendProvider.OPENAI,
        model="gpt-4o-mini",
        api_key="test-key",
    )
    backend = create_backend(config)
    assert isinstance(backend, OpenAIAgent)
    caps = backend.capabilities()
    assert caps.supports_tool_calling
    assert caps.supports_json_mode


def test_create_anthropic_backend():
    config = BackendConfig(
        provider=BackendProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        api_key="test-key",
    )
    backend = create_backend(config)
    assert isinstance(backend, AnthropicAgent)
    caps = backend.capabilities()
    assert caps.supports_tool_calling
    assert caps.supports_thinking_level


def test_create_unsupported_provider():
    class FakeProvider:
        pass
    config = BackendConfig(
        provider=FakeProvider(),
        api_key="test-key",
    )
    with pytest.raises(ValueError, match="不支持的后端提供商"):
        create_backend(config)