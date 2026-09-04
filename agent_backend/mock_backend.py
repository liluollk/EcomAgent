"""
Mock Backend — 用于离线演示、联调与测试的脚本化后端。

根据用户消息中的关键词 scripted 出对应的工具调用与文本回复，
无需真实 LLM API Key 即可跑通完整的
"工具调用 → 权限确认 → 结果回传 → 继续推理" 闭环。
通过 AGENT_BACKEND=mock 环境变量启用。
"""

from __future__ import annotations

import re
from typing import AsyncGenerator

from events.agent_event import (
    AgentEvent,
    TextDeltaEvent,
    ToolStartEvent,
    StatusEvent,
    TypedErrorEvent,
    TypedError,
)
from .protocol import AgentBackend, BackendConfig, AgentCapabilities, BackendProvider


def _extract_channel(text: str, default: str = "taobao") -> str:
    """从用户文本识别渠道意图。

    已知别名（淘宝/京东/抖音/拼多多及拼音）优先；否则匹配 "xx 渠道" 形态，
    若该候选已在渠道注册表启用则采用（支持设置里动态新增的渠道），
    否则回退 default（保持既有分支默认语义）。
    """
    aliases = {
        "淘宝": "taobao", "taobao": "taobao",
        "京东": "jd", "jd": "jd",
        "抖音": "douyin", "douyin": "douyin",
        "拼多多": "pdd", "pdd": "pdd",
    }
    for key, channel in aliases.items():
        if key in text:
            return channel
    m = re.search(r"([A-Za-z0-9_-]+)\s*渠道", text)
    if m:
        try:
            from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

            candidate = m.group(1)
            if candidate in DEFAULT_CHANNEL_REGISTRY.enabled_names():
                return candidate
        except Exception:
            pass
    return default


class MockAgent:
    """脚本化 Mock 后端，事件语义与真实后端保持一致。"""

    def __init__(self, config: BackendConfig) -> None:
        """初始化 Mock 后端。

        Args:
            config: 后端配置。
        """
        self._config = config
        self._aborted = False

    def capabilities(self) -> AgentCapabilities:
        """返回 Mock 后端能力声明。"""
        return AgentCapabilities(
            supports_tool_calling=True,
            supports_thinking_level=False,
            supports_json_mode=False,
            context_window=32000,
        )

    def get_config(self) -> BackendConfig:
        """获取当前后端配置。"""
        return self._config

    def update_runtime_config(self, config: BackendConfig) -> None:
        """更新运行时配置。

        Args:
            config: 新的后端配置。
        """
        self._config = config

    def abort(self, reason: str) -> None:
        """设置中断标志。"""
        self._aborted = True

    async def chat(
        self, messages: list[dict], tools: list[dict], session_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        """按最后一条用户消息的关键词 scripted 事件流（模拟渐进式技能加载）。

        三阶段剧本：
        1. 第一轮（用户消息结尾）：根据关键词映射技能 → yield load_skill 调用
        2. 第二轮（load_skill 结果结尾）：yield 该技能的 domain 工具调用
        3. 第三轮（domain 结果结尾）：产出总结文本
        """
        self._aborted = False
        try:
            yield StatusEvent(message="Mock 后端执行中...")
            if messages and messages[-1].get("role") == "tool":
                last_result = str(messages[-1].get("content", ""))
                if "已加载技能" in last_result:
                    # 第二轮：技能已加载，产出 domain 工具调用
                    yield TextDeltaEvent(text="好的，我来调用该技能对应的工具。")
                    yield self._domain_tool_start(messages)
                    return
                # 第三轮：domain 工具已执行，产出总结
                yield TextDeltaEvent(text="已根据工具返回结果完成本次操作，请查收。")
                return

            last_user = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            skill_name = self._skill_for(last_user)
            yield TextDeltaEvent(text="好的，我先加载对应的技能。")
            yield ToolStartEvent(
                tool_name="load_skill",
                tool_use_id="call_mock_load_skill",
                input={"skill_name": skill_name},
            )
        except Exception as e:
            yield TypedErrorEvent(
                error=TypedError(
                    code="MOCK_BACKEND_ERROR",
                    title="Mock 后端执行失败",
                    message=str(e),
                    can_retry=False,
                )
            )

    @staticmethod
    def _skill_for(user_message: str) -> str:
        """按关键词映射技能名（与内置技能 keywords 对齐）。"""
        if any(w in user_message for w in ("促销", "优惠", "折扣", "满减")):
            return "promotion_management"
        if any(w in user_message for w in ("价格", "调价", "改价", "定价", "降价")):
            return "price_management"
        if any(w in user_message for w in ("知识", "规范", "话术", "政策")):
            return "knowledge_inquiry"
        if any(w in user_message for w in ("分析", "销售额", "GMV", "成交")):
            return "order_analytics"
        if any(w in user_message for w in ("异常", "预警", "风控")):
            return "anomaly_detection"
        if any(w in user_message for w in ("上架", "下架", "下柜")):
            return "product_listing"
        if any(w in user_message for w in ("工单", "客诉")):
            return "after_sales"
        if any(w in user_message for w in ("订单", "发货", "物流")):
            return "order_management"
        return "inventory_query"

    def _domain_tool_start(self, messages: list[dict]) -> ToolStartEvent:
        """第二轮：根据用户消息关键词构造 domain 工具调用（与旧剧本一致）。"""
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        if "促销" in last_user or "活动" in last_user:
            return ToolStartEvent(
                tool_name="create_promotion",
                tool_use_id="call_mock_promo",
                input={
                    "channel": _extract_channel(last_user, default="douyin"),
                    "sku": "SKU-003",
                    "discount": 0.8,
                    "start_time": "2026-09-01 00:00:00",
                    "end_time": "2026-09-07 23:59:59",
                },
            )
        if "价格" in last_user or "调价" in last_user or "改价" in last_user:
            m_price = re.search(r"(?:改为|调到|改成)\s*(\d+(?:\.\d+)?)", last_user)
            m_cost = re.search(r"成本\s*(\d+(?:\.\d+)?)", last_user)
            new_price = float(m_price.group(1)) if m_price else 79.0
            cost_price = float(m_cost.group(1)) if m_cost else 59.0
            return ToolStartEvent(
                tool_name="update_price",
                tool_use_id="call_mock_price",
                input={
                    "channel": _extract_channel(last_user),
                    "sku": "SKU-001",
                    "new_price": new_price,
                    "cost_price": cost_price,
                },
            )
        if "知识" in last_user or "话术" in last_user or "政策" in last_user or "规范" in last_user:
            return ToolStartEvent(
                tool_name="query_knowledge_base",
                tool_use_id="call_mock_kb",
                input={"topic": "上架规范"},
            )
        if "分析" in last_user or "销售额" in last_user or "GMV" in last_user or "成交" in last_user:
            return ToolStartEvent(
                tool_name="query_order_stats",
                tool_use_id="call_mock_stats",
                input={"channel": _extract_channel(last_user), "period": "近7天"},
            )
        if "异常" in last_user or "预警" in last_user:
            return ToolStartEvent(
                tool_name="query_anomalies",
                tool_use_id="call_mock_anomaly",
                input={"channel": _extract_channel(last_user)},
            )
        if "上架" in last_user or "下架" in last_user or "下柜" in last_user:
            return ToolStartEvent(
                tool_name="product_shelf",
                tool_use_id="call_mock_shelf",
                input={"channel": _extract_channel(last_user), "sku": "SKU-001", "action": "off"},
            )
        if "工单" in last_user or "客诉" in last_user:
            return ToolStartEvent(
                tool_name="service_ticket",
                tool_use_id="call_mock_ticket",
                input={"channel": _extract_channel(last_user), "order_id": "TB-10086", "issue": "商品破损，申请退货退款", "priority": "high"},
            )
        if "订单" in last_user:
            return ToolStartEvent(
                tool_name="query_order_status",
                tool_use_id="call_mock_order",
                input={"channel": _extract_channel(last_user), "order_id": "TB-10086"},
            )
        return ToolStartEvent(
            tool_name="query_inventory",
            tool_use_id="call_mock_inv",
            input={"channel": _extract_channel(last_user), "sku": "SKU-001"},
        )
