"""上下文压缩策略 — 会话超长时折叠早期消息为结构化摘要注入。

设计（对应真实接入路径，不在技术选型上设限）：
- 触发阈值动态化：`模型上下文窗口 - 13k 安全边际`。后端不同（mock=32k /
  OpenAI=128k / Anthropic=200k）时阈值自动跟随；预设安全边际 13k。
- 自动压缩：消息总字符超过阈值时，早期消息折叠为一条 system 摘要置于最前，
  保留最近 N 条原文；保证折叠后近期首条不为裸 tool 消息（API 结构校验）。
- 手动压缩：调用方（WS compact 通道 / 前端按钮）可随时折叠当前会话。
- 结构化摘要：目标 / 决策 / 进度 / 当前做到哪里 / 读了什么用了什么工具。
- 摘要生成器可注入：真实项目接入真实 LLM 摘要（未来接真实 API 时传入
  summarizer 即可）；未注入时用规则提取器兜底（离线 / 测试可用）。
"""

from __future__ import annotations

import json as _json
from typing import Any, Callable, Optional

# 模型上下文窗口预留的安全边际（token ≈ 13k，字符口径保守取 13k）
DEFAULT_SAFE_MARGIN = 13000
# 未提供窗口时的兜底阈值（对齐 mock 后端 32k - 13k）
DEFAULT_MAX_CONTEXT_CHARS = 19000
# 压缩后保留的最近消息条数（原文）
DEFAULT_KEEP_RECENT = 8
# 结构化摘要中各摘录字段的最大字符数
EXCERPT_CHARS = 140


def context_threshold(context_window: int, margin: int = DEFAULT_SAFE_MARGIN) -> int:
    """由模型上下文窗口计算压缩触发阈值（字符口径）。"""
    return max(context_window - margin, 1000)


class ContextCompressor:
    """会话消息折叠器：结构化摘要 + 自动/手动两种触发。"""

    def __init__(
        self,
        threshold_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        summarizer: Optional[Callable[[list[dict],], str]] = None,
    ) -> None:
        """初始化压缩器。

        Args:
            threshold_chars: 触发压缩的消息总字符阈值（由窗口 - 13k 计算）。
            keep_recent: 保留的最近消息条数。
            summarizer: 可选摘要生成器（入参早期消息列表，返回摘要文本）。
                缺省使用结构化规则提取。真实接入时传入 LLM 摘要实现。
        """
        self._threshold = threshold_chars
        self._keep_recent = keep_recent
        self._summarizer = summarizer or self._structured_summary

    @property
    def threshold_chars(self) -> int:
        return self._threshold

    # ------------------------------------------------------------------
    # 压缩入口
    # ------------------------------------------------------------------

    def should_collapse(self, messages: list[dict[str, Any]]) -> bool:
        """是否应压缩（自动触发判定）。"""
        if len(messages) <= self._keep_recent + 1:
            return False
        return self._content_chars(messages) > self._threshold

    def collapse(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """折叠超长会话（自动压缩）。未超限时原样返回（不改动调用方对象）。"""
        if not self.should_collapse(messages):
            return messages, False
        return self._fold(messages), True

    def collapse_session_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """手动压缩：折叠 Session.messages（含 timestamp 的持久化条目）。

        早期条目折叠为一条 system 摘要（保留 timestamp），近期原文保留，
        返回可直接写回 session 的新列表。
        """
        if len(entries) <= self._keep_recent + 1:
            return entries
        return self._fold_entries(entries)

    # ------------------------------------------------------------------
    # 折叠实现
    # ------------------------------------------------------------------

    def _fold(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        early = list(messages[: -self._keep_recent])
        kept = list(messages[-self._keep_recent:])
        # 摘要基于**原始**早期区生成（先于结构补位，避免丢失工具轨迹信息）
        summary_text = self._summarizer(list(early))
        # 结构修正：保留窗口首条为 tool 时补齐其 assistant(tool_calls)
        while kept and kept[0].get("role") == "tool":
            if early:
                kept.insert(0, early.pop())
            else:
                kept.pop(0)
        return [{"role": "system", "content": summary_text}] + kept

    def _fold_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        early = list(entries[: -self._keep_recent])
        kept = list(entries[-self._keep_recent:])
        summary_text = self._summarizer(list(early))
        while kept and kept[0].get("role") == "tool":
            if early:
                kept.insert(0, early.pop())
            else:
                kept.pop(0)
        summary_entry = {
            "role": "system",
            "content": summary_text,
            "timestamp": kept[0].get("timestamp") if kept else "",
        }
        return [summary_entry] + kept

    @staticmethod
    def _content_chars(messages: list[dict[str, Any]]) -> int:
        total = 0
        for m in messages:
            total += len(m.get("content") or "")
            for tc in m.get("tool_calls") or []:
                total += len(str(tc.get("function", {}).get("arguments", "")))
        return total

    # ------------------------------------------------------------------
    # 结构化摘要（规则提取，LLM 摘要可替换）
    # ------------------------------------------------------------------

    def _structured_summary(self, early_messages: list[dict[str, Any]]) -> str:
        """从早期消息提取结构化要点：目标 / 决策 / 进度 / 当前状态 / 工具。"""
        tool_names: list[str] = []
        tool_args: list[str] = []
        last_user = ""
        last_result = ""
        for m in early_messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {}) or {}
                    name = str(fn.get("name", "") or "?")
                    if name not in tool_names:
                        tool_names.append(name)
                    try:
                        args = _json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    channel = args.get("channel") or ""
                    if name and channel and f"{name}({channel})" not in tool_args:
                        tool_args.append(f"{name}(渠道 {channel})")
            elif m.get("role") == "user" and m.get("content"):
                last_user = str(m["content"])
            elif m.get("role") == "tool" and m.get("content"):
                last_result = str(m["content"])

        lines = []
        if last_user:
            lines.append(f"目标：{last_user[:EXCERPT_CHARS]}")
        if tool_names:
            lines.append(f"已用工具：{'、'.join(tool_names)}")
        if tool_args:
            lines.append(f"涉及渠道/操作：{'；'.join(tool_args[:5])}")
        if last_result:
            lines.append(f"进度（最后结果）：{last_result[:EXCERPT_CHARS]}")
        lines.append("当前状态：早期对话已压缩为摘要，原文不再参与后续推理")
        if len(lines) <= 1:
            lines.insert(0, "决策：早期对话无显著决策点")
        body = "\n".join(lines)
        return "[会话摘要·结构化]（早期对话已压缩）\n" + body