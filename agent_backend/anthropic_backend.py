"""
Anthropic Backend — 基于 Anthropic API 的后端实现。

AnthropicAgent（ClaudeAgent）。
通过 AsyncAnthropic SDK 实现流式对话，将 Anthropic 事件格式统一适配为 AgentEvent。
关键差异：Anthropic 流式中 tool_use 的 input 是增量 JSON 片段（input_json_delta），
需要累积后在 content_block_stop 时解析完整 JSON。
"""

from __future__ import annotations

import json as _json
from typing import AsyncGenerator

from anthropic import AsyncAnthropic

from events.agent_event import (
    AgentEvent,
    TextDeltaEvent,
    ToolStartEvent,
    StatusEvent,
    TypedErrorEvent,
    TypedError,
)
from .protocol import AgentBackend, BackendConfig, AgentCapabilities, BackendProvider


def _normalize_openai_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """将内部统一使用的 OpenAI 消息格式归一化为 Anthropic 格式。

    转换规则：
    - role="system" → 提取文本合并为 system 提示词（通过返回值第二项带出）
    - role="user" → user 文本消息
    - role="assistant"（含 tool_calls）→ assistant 消息，text + tool_use 内容块
    - role="tool" → user 消息中的 tool_result 内容块，连续多条合并为一条 user 消息

    Args:
        messages: 内部 OpenAI 格式消息列表。

    Returns:
        tuple[list[dict], str]: (Anthropic 格式消息列表, 提取出的 system 文本)。
    """
    system_parts: list[str] = []
    normalized: list[dict] = []
    pending_tool_results: list[dict] = []

    def _flush_tool_results() -> None:
        if pending_tool_results:
            normalized.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue

        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else str(content),
            })
            continue

        _flush_tool_results()

        if role == "assistant":
            blocks: list[dict] = []
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    tool_input = _json.loads(fn.get("arguments") or "{}")
                except _json.JSONDecodeError:
                    tool_input = {}
                if not isinstance(tool_input, dict):
                    tool_input = {"raw": tool_input}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": tool_input,
                })
            if blocks:
                normalized.append({"role": "assistant", "content": blocks})
            # blocks 为空（无文本也无工具调用）时跳过，避免空内容块被 API 拒绝
        else:
            text = content if isinstance(content, str) else str(content)
            normalized.append({"role": "user", "content": [{"type": "text", "text": text}]})

    _flush_tool_results()
    return normalized, "\n\n".join(system_parts)


class AnthropicAgent:
    """Anthropic 后端实现。

    使用 Anthropic Python SDK 的 AsyncAnthropic 客户端进行流式 API 调用。
    支持 tool use，正确处理 Anthropic 流式 API 中 tool_use input 的增量累积。
    """

    # 统一 thinking_level → Anthropic thinking.budget_tokens 映射
    _THINKING_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384}

    def __init__(self, config: BackendConfig) -> None:
        """初始化 Anthropic 后端。

        Args:
            config: 后端配置，包含 API key、模型名等。
        """
        self._config = config
        self._aborted = False
        # token 用量累计（真实模型评测成本维度；reset_usage 按回合清零）
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._client = AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.api_base,
        )

    @property
    def usage(self) -> dict:
        """本回合累计 token 用量（prompt/completion）。"""
        return dict(self._usage)

    def reset_usage(self) -> None:
        """清零用量累计（BaseAgent 每回合开始时调用）。"""
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def capabilities(self) -> AgentCapabilities:
        """返回 Anthropic 后端能力声明。

        Returns:
            AgentCapabilities: 支持 tool_calling 和 thinking_level。
        """
        return AgentCapabilities(
            supports_tool_calling=True,
            supports_thinking_level=True,
            supports_json_mode=False,
            context_window=200000,
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
        self._client = AsyncAnthropic(
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
        """流式对话，将消息列表发送到 Anthropic API。

        Anthropic 流式 API 中 tool_use 的 input 是增量 JSON 片段：
        - content_block_start(type="tool_use") → 记录 tool_name, tool_id
        - content_block_delta(delta.type="input_json_delta") → 累积 JSON 片段
        - content_block_stop → 解析完整 JSON，发出 ToolStartEvent

        Args:
            messages: 消息列表（内部 OpenAI 格式，发送前归一化为 Anthropic 格式）。
            tools: 工具定义列表（OpenAI function calling 格式）。
            session_id: 会话 ID，用于日志追踪。

        Yields:
            AgentEvent: 流式事件。
        """
        self._aborted = False

        anthropic_tools = [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", t.get("input_schema", {})),
            }
            for t in tools
        ] if tools else None

        # OpenAI → Anthropic 消息归一化：tool_calls/tool 消息转为 tool_use/tool_result 内容块
        anthropic_messages, extra_system = _normalize_openai_messages(messages)
        if extra_system:
            # 当消息本身携带 system blocks 时，以消息为唯一事实源，
            # 避免 BackendConfig.system_prompt 再复制一份上下文。
            system = extra_system
        else:
            system = self._config.system_prompt or "You are a helpful assistant."

        try:
            yield StatusEvent(message="正在调用 Anthropic API...")
            # 统一 thinking_level → Anthropic thinking 参数。
            # 启用 thinking 时 API 要求 temperature=1，否则 400；
            # 未启用时不传 thinking 键（显式 null 可能被部分网关拒绝）。
            thinking_level = self._config.thinking_level
            thinking_params = None
            if thinking_level in self._THINKING_BUDGETS:
                thinking_params = {
                    "type": "enabled",
                    "budget_tokens": self._THINKING_BUDGETS[thinking_level],
                }
            request_kwargs: dict = {
                "model": self._config.model,
                "max_tokens": self._config.max_output_tokens,
                "system": system,
                "messages": anthropic_messages,
                "tools": anthropic_tools,
                "temperature": (
                    1 if thinking_params else self._config.temperature
                ),
            }
            if thinking_params:
                request_kwargs["thinking"] = thinking_params
            async with self._client.messages.stream(**request_kwargs) as stream:
                tool_inputs: dict[int, str] = {}
                tool_names: dict[int, str] = {}
                tool_ids: dict[int, str] = {}

                async for event in stream:
                    if self._aborted:
                        break

                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield TextDeltaEvent(text=event.delta.text)
                        elif event.delta.type == "input_json_delta":
                            tool_inputs[event.index] = tool_inputs.get(event.index, "") + event.delta.partial_json

                    elif event.type == "content_block_start":
                        if event.content_block.type == "tool_use":
                            tool_inputs[event.index] = ""
                            tool_names[event.index] = event.content_block.name
                            tool_ids[event.index] = event.content_block.id

                    elif event.type == "content_block_stop":
                        if event.index in tool_inputs:
                            raw = tool_inputs.pop(event.index)
                            name = tool_names.pop(event.index, "unknown")
                            tid = tool_ids.pop(event.index, f"tool_{event.index}")
                            try:
                                parsed = _json.loads(raw) if raw.strip() else {}
                            except _json.JSONDecodeError:
                                parsed = {"raw": raw}
                            yield ToolStartEvent(
                                tool_name=name,
                                tool_use_id=tid,
                                input=parsed,
                            )

                # 流结束后取最终 usage（input/output tokens），供成本维度统计
                try:
                    final_usage = stream.get_final_usage()
                    if final_usage is not None:
                        self._usage["prompt_tokens"] += getattr(final_usage, "input_tokens", 0) or 0
                        self._usage["completion_tokens"] += getattr(final_usage, "output_tokens", 0) or 0
                except Exception:
                    pass

        except Exception as e:
            yield TypedErrorEvent(
                error=TypedError(
                    code="ANTHROPIC_API_ERROR",
                    title="Anthropic API 调用失败",
                    message=str(e),
                    can_retry=True,
                )
            )
