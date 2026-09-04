"""
后端工厂 — 根据 BackendConfig.provider 创建对应的后端实例。

createBackend() 工厂函数。
简化版：直接根据 provider 枚举选择后端，不依赖 CLI 运行时路径解析。
"""

from __future__ import annotations

from .protocol import BackendConfig, BackendProvider
from .openai_backend import OpenAIAgent
from .anthropic_backend import AnthropicAgent
from .mock_backend import MockAgent


def create_backend(config: BackendConfig) -> OpenAIAgent | AnthropicAgent | MockAgent:
    """根据配置创建对应的后端实例。

    Args:
        config: 后端配置，provider 字段决定创建哪种后端。

    Returns:
        OpenAIAgent | AnthropicAgent | MockAgent: 后端实例。

    Raises:
        ValueError: 当 provider 不受支持时。
    """
    if config.provider == BackendProvider.OPENAI:
        return OpenAIAgent(config)
    elif config.provider == BackendProvider.ANTHROPIC:
        return AnthropicAgent(config)
    elif config.provider == BackendProvider.MOCK:
        return MockAgent(config)
    else:
        raise ValueError(f"不支持的后端提供商: {config.provider}")