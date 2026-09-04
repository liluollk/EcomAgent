"""
AgentBackend 协议 — 统一多模型后端的接口契约。

AgentBackend interface。
使用 Python Protocol（结构化子类型）定义后端接口，
AnyOpenAI/AnyAnthropic 等后端实现此协议，AgentBackend 不感知具体模型差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import AsyncGenerator, Optional, Protocol

from events.agent_event import AgentEvent


class BackendProvider(Enum):
    """后端提供商枚举。"""

    OPENAI = auto()
    ANTHROPIC = auto()
    MOCK = auto()


@dataclass
class AgentCapabilities:
    """后端能力声明 — 用于驱动 UI 显示和运行时决策。

    Attributes:
        supports_tool_calling: 是否支持工具调用（function calling / tool use）。
        supports_thinking_level: 是否支持思考模式（extended thinking）。
        supports_json_mode: 是否支持 JSON 结构化输出模式。
        context_window: 上下文窗口大小（token 数）。
    """

    supports_tool_calling: bool = True
    supports_thinking_level: bool = False
    supports_json_mode: bool = False
    context_window: int = 128000


@dataclass
class BackendConfig:
    """后端配置 — 包含模型选择、API 密钥和运行时参数。

    Attributes:
        provider: 后端提供商。
        model: 模型名称，如 "gpt-4o-mini"、"claude-sonnet-4-20250514"。
        api_key: API 密钥。
        api_base: 自定义 API 基础 URL（私有部署 / 自定义网关地址）。
        thinking_level: 思考强度级别（None=关闭，或 "low"/"medium"/"high"）。
            供应商无关的统一字段，由各 adapter 翻译为自家协议参数：
            Anthropic → thinking.budget_tokens；OpenAI 推理模型 → reasoning_effort。
        system_prompt: 系统提示词。
        temperature: 温度参数，控制输出随机性。
        max_output_tokens: 最大输出 token 数。
    """

    provider: BackendProvider
    model: str = "gpt-4o-mini"
    api_key: str = ""
    api_base: Optional[str] = None
    thinking_level: Optional[str] = None
    system_prompt: str = ""
    temperature: float = 0.7
    max_output_tokens: int = 4096


class AgentBackend(Protocol):
    """AgentBackend 协议 — 所有后端实现的结构化类型契约。

    AgentBackend 接口 — 所有后端实现的结构化类型契约。
    使用 Protocol 而非 ABC，允许通过结构化子类型而非显式继承来满足接口。
    后端实现无需继承此类，只需实现协议中定义的方法即可。

    核心方法：
        chat: 流式对话，返回 AgentEvent 的 AsyncGenerator。
        abort: 中断当前对话。
        capabilities: 返回后端能力声明。
        get_config: 获取当前后端配置。
        update_runtime_config: 更新运行时配置（如系统提示词）。
    """

    async def chat(
        self, messages: list[dict], tools: list[dict], session_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        """流式对话，将消息列表转换为 AgentEvent 事件流。

        Args:
            messages: 消息列表，每项包含 role 和 content。
            tools: 工具定义列表（OpenAI/Anthropic 格式）。
            session_id: 会话 ID，用于日志追踪。

        Yields:
            AgentEvent: 流式事件（text_delta、tool_start、typed_error 等）。
        """
        ...

    def abort(self, reason: str) -> None:
        """中断当前对话。

        Args:
            reason: 中断原因，如 "user_cancel"。
        """
        ...

    def capabilities(self) -> AgentCapabilities:
        """返回后端能力声明。

        Returns:
            AgentCapabilities: 后端能力。
        """
        ...

    def get_config(self) -> BackendConfig:
        """获取当前后端配置。

        Returns:
            BackendConfig: 当前配置。
        """
        ...

    def update_runtime_config(self, config: BackendConfig) -> None:
        """更新运行时配置。

        Args:
            config: 新的后端配置。
        """
        ...