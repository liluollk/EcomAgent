"""
OpenAI Backend — 基于 OpenAI 兼容 API 的后端实现。

OpenAIAgent。
通过 AsyncOpenAI SDK 实现流式对话，将 OpenAI 事件格式统一适配为 AgentEvent。
"""

from __future__ import annotations

import json as _json

from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI

from events.agent_event import (
    AgentEvent,
    TextDeltaEvent,
    ToolStartEvent,
    ToolResultEvent,
    StatusEvent,
    TypedErrorEvent,
    TypedError,
)
from .protocol import AgentBackend, BackendConfig, AgentCapabilities, BackendProvider


class OpenAIAgent:
    """OpenAI 兼容后端实现。

    使用 OpenAI Python SDK 的 AsyncOpenAI 客户端进行流式 API 调用。
    支持 function calling，将 tool_calls 事件转换为 ToolStartEvent。

    注意：OpenAI 流式协议中 tool_calls 的 function.arguments 是跨 chunk 增量
    传输的，必须按 index 累积，待流结束后解析完整 JSON 再发出 ToolStartEvent。
    """

    def __init__(self, config: BackendConfig) -> None:
        """初始化 OpenAI 后端。

        Args:
            config: 后端配置，包含 API key、模型名等。
        """
        self._config = config
        self._aborted = False
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.api_base,
        )

    # 支持 reasoning_effort 参数的 OpenAI 推理模型前缀（o 系列 / gpt-5 系列）
    _THINKING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")

    def capabilities(self) -> AgentCapabilities:
        """返回 OpenAI 后端能力声明。

        Returns:
            AgentCapabilities: 支持 tool_calling 和 json_mode；
                supports_thinking_level 按模型名启发式判断（推理模型）。
        """
        model = (self._config.model or "").strip().lower()
        supports_thinking = model.startswith(self._THINKING_MODEL_PREFIXES)
        return AgentCapabilities(
            supports_tool_calling=True,
            supports_thinking_level=supports_thinking,
            supports_json_mode=True,
            context_window=128000,
        )

    def get_config(self) -> BackendConfig:
        """获取当前后端配置。

        Returns:
            BackendConfig: 当前配置。
        """
        return self._config

    def update_runtime_config(self, config: BackendConfig) -> None:
        """更新运行时配置并重新创建客户端。

        Args:
            config: 新的后端配置。
        """
        self._config = config
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.api_base,
        )

    def abort(self, reason: str) -> None:
        """设置中断标志，流式循环检测到后停止产出事件。

        Args:
            reason: 中断原因。
        """
        self._aborted = True

    async def chat(
        self, messages: list[dict], tools: list[dict], session_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        """流式对话，将消息列表发送到 OpenAI API。

        将 OpenAI 的 ChatCompletionChunk 事件转换为 AgentEvent：
        - delta.content → TextDeltaEvent
        - 累积完成的 delta.tool_calls → ToolStartEvent
        - 异常 → TypedErrorEvent

        Args:
            messages: 消息列表，每项包含 role 和 content。
            tools: 工具定义列表（OpenAI function calling 格式）。
            session_id: 会话 ID，用于日志追踪。

        Yields:
            AgentEvent: 流式事件。
        """
        self._aborted = False
        # 将工具定义转换为 OpenAI function calling 格式
        openai_tools = [
            {"type": "function", "function": t} for t in tools
        ] if tools else None

        try:
            yield StatusEvent(message="正在调用 OpenAI API...")
            # 统一 thinking_level → OpenAI reasoning_effort（仅推理模型有效；
            # low/medium/high 恰好是双方一致的取值）
            thinking_level = self._config.thinking_level
            create_kwargs: dict = {}
            if thinking_level in ("low", "medium", "high"):
                create_kwargs["reasoning_effort"] = thinking_level
            stream = await self._client.chat.completions.create(
                model=self._config.model,
                messages=messages,
                tools=openai_tools,
                temperature=self._config.temperature,
                stream=True,
                stream_options={"include_usage": False},
                **create_kwargs,
            )
            pending_calls: dict[int, dict] = {}
            async for chunk in stream:
                if self._aborted:
                    break
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is None:
                    continue
                # 推理模型流式输出中的 reasoning_content 是思考过程，
                # 不进入对话文本（避免污染最终回复）
                if getattr(delta, "reasoning_content", None):
                    continue
                if delta.content:
                    yield TextDeltaEvent(text=delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        slot = pending_calls.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments

            # 流结束后解析累积完成的工具调用
            if not self._aborted:
                for idx in sorted(pending_calls):
                    slot = pending_calls[idx]
                    try:
                        input_data = (
                            _json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
                        )
                    except _json.JSONDecodeError:
                        input_data = {"raw": slot["arguments"]}
                    yield ToolStartEvent(
                        tool_name=slot["name"],
                        tool_use_id=slot["id"] or f"call_{idx}",
                        input=input_data,
                    )
        except Exception as e:
            yield TypedErrorEvent(
                error=TypedError(
                    code="OPENAI_API_ERROR",
                    title="OpenAI API 调用失败",
                    message=str(e),
                    can_retry=True,
                )
            )
