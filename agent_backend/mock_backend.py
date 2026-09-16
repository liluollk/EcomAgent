"""
Mock Backend — 用于离线演示、联调与测试的脚本化后端。

根据用户消息中的关键词 scripted 出对应的工具调用与文本回复，
无需真实 LLM API Key 即可跑通完整的
"工具调用 → 权限确认 → 结果回传 → 继续推理" 闭环。
通过 AGENT_BACKEND=mock 环境变量启用。

收尾回合不是固定话术：按真实模型的行为，读取本轮工具名 / 入参与
工具结果文本，组织引用关键事实、给出下一步建议的自然语言总结。
"""

from __future__ import annotations

import asyncio
import json
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

_CHANNEL_CN = {"taobao": "淘宝", "jd": "京东", "douyin": "抖音", "pdd": "拼多多"}

# 平台（调价闭环的领域标识）→ 中文标签
_PLATFORM_CN = {"taobao": "淘宝", "douyin": "抖店", "jd": "京东", "mock": "本地 Mock"}

# 结果前缀 → 视为执行失败（错误文本随成功通道回传，由文案前缀区分）
# 「拦截 / 未生效 / 未确认」都是调价工具的正常返回文本（工具不抛异常），
# 但语义上是失败，收尾总结必须按失败处理，不能当成改价成功。
_ERROR_PREFIXES = (
    "[平台错误", "[执行超时", "[上游响应异常", "[已拒绝", "[执行异常",
    "拦截：", "调价未生效", "调价结果未确认",
)


def _last_tool_context(messages: list[dict]) -> tuple[str, dict, str]:
    """提取本轮（domain）工具的 (工具名, 入参, 结果文本)。"""
    result = str(messages[-1].get("content", ""))
    name, args = "", {}
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            fn = (m["tool_calls"][-1].get("function") or {})
            name = str(fn.get("name", ""))
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            break
    return name, args, result


def _platform_label(args: dict) -> str:
    """调价工具的领域引用标签：平台 + 商品 / SKU。"""
    platform = str(args.get("platform", ""))
    label = _PLATFORM_CN.get(platform, platform)
    product = str(args.get("product_id", ""))
    sku = str(args.get("sku_id", ""))
    return f"{label} {product}/{sku}".strip()


def _summarize(tool_name: str, args: dict, result: str) -> str:
    """按工具类型把结果文本组织成收尾总结：复述关键事实 + 下一步建议。"""
    if result.startswith(_ERROR_PREFIXES):
        if result.startswith("[已拒绝"):
            return (
                "这个操作被拒绝了，我不会继续执行。"
                "可以调整方案再来一次——比如先查询确认现状，或者换一个商品 / 渠道。"
            )
        if result.startswith("调价结果未确认"):
            # 写入已发出但回查没跑完：不能声称成功，也不能声称没执行
            return (
                f"这次调价没能确认结果：{result}"
                "为避免在不确定的状态上继续操作，我已停止后续动作。"
                "建议先重新查一次当前价，确认实际状态后再决定是否重做。"
            )
        if result.startswith("调价未生效"):
            return (
                f"这次调价没有生效：{result}"
                "平台写入与回查结论不一致，我不会把它当作已完成。"
                "建议核对活动/规则后再提一次，或换一个 SKU 试试。"
            )
        return (
            f"这次没有执行成功：{result}"
            "为避免在不确定的状态上继续操作，我已停止后续动作。"
            "建议稍后重试，或告诉我改用其他平台完成。"
        )

    if tool_name == "query_product_snapshot":
        m = re.search(r"当前价 ([\d.]+) 元，库存 (\d+) 件，状态 (\S+?)([；。]|$)", result)
        if m:
            price, stock = m.group(1), int(m.group(2))
            advice = "库存偏紧，建议尽快安排补货" if stock < 20 else "库存可以支撑近期销售"
            activity = ""
            m_act = re.search(r"进行中活动：(.+?)(（|；|$)", result)
            if m_act:
                activity = f"当前在「{m_act.group(1)}」活动中，改价可能被平台锁价拦住；"
            return (
                f"查询完成：{_platform_label(args)} 当前价 {price} 元、库存 {stock} 件，{advice}。"
                f"{activity}需要的话我可以按新目标价提交这次调价。"
            )
        if "暂不可查询" in result:
            return (
                f"这个商品我没查到：{result}"
                "请确认平台、商品号与 SKU 是否正确；如果平台尚未接入调价通道，我也不会做任何写操作。"
            )
        return f"查询完成：{result}"

    if tool_name == "update_price":
        m = re.search(r"价格已更新为 ([\d.]+) 元", result)
        if m:
            replay = "本次结果为重试后的幂等回放，平台没有重复执行调价。" if "幂等" in result else ""
            return (
                f"调价完成：{_platform_label(args)} 价格已更新为 {m.group(1)} 元，并已回查平台确认一致。"
                f"{replay}建议接下来几天关注该商品的转化率变化，确认调价效果。"
            )
        return f"调价结果：{result}"

    ch = _CHANNEL_CN.get(str(args.get("channel", "")), str(args.get("channel", "")))
    sku = str(args.get("sku", ""))

    if tool_name == "query_inventory":
        m = re.search(r"库存 (\d+) 件：(.+)$", result)
        if m:
            stock, name = int(m.group(1)), m.group(2)
            advice = "库存偏紧，建议尽快安排补货" if stock < 20 else "库存可以支撑近期销售"
            return (
                f"查询完成：{ch}渠道 {sku}「{name}」当前库存 {stock} 件，{advice}。"
                "需要的话我可以顺手对比一下这款在其他渠道的库存。"
            )

    if tool_name == "create_promotion":
        discount = args.get("discount")
        off = f"{discount * 10:g} 折" if isinstance(discount, (int, float)) else "限时"
        return (
            f"促销已上线：{ch}渠道 {sku} 的 {off}活动将从 {args.get('start_time', '')} 开始，"
            f"到 {args.get('end_time', '')} 结束。"
            "活动期间折扣力度较大，建议同步确认库存深度，避免超卖。"
        )

    if tool_name == "query_order_status":
        m = re.search(r"状态: (.+)$", result)
        status = m.group(1) if m else "未知"
        return (
            f"订单 {args.get('order_id', '')} 当前状态：{status}。"
            "如果后续需要跟踪物流异常或发起售后，随时告诉我。"
        )

    if tool_name == "product_shelf":
        action = "上架" if "已上架" in result else "下架"
        tail = (
            "商品恢复可见后，建议确认价格与促销信息是否已同步。" if action == "上架"
            else "下架后前台将不可购买，库存与评价数据会保留。"
        )
        return f"操作完成：{ch}渠道 {sku} 已{action}。{tail}"

    if tool_name == "query_order_stats":
        return (
            f"统计完成：{result}。"
            "整体数据已拉齐，建议结合退款率与异常项一起看，判断是否需要调整投放。"
        )

    if tool_name == "query_anomalies":
        if "经营状态正常" in result:
            return f"排查完成：{ch or '各'}渠道目前没有需要处理的异常项，经营状态正常。"
        return f"排查完成：{result}。建议优先处理影响库存与价格展现的异常项。"

    if tool_name == "save_skill":
        return f"技能「{args.get('name', '')}」已保存并热加载生效，下次输入 /{args.get('name', '')} 即可直达该流程。"

    return f"已经完成你交办的操作：{result}。还需要处理其他渠道或商品的话，随时告诉我。"


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


def _extract_platform(text: str, default: str = "") -> str:
    """从用户文本识别平台（调价闭环的领域标识：taobao / douyin / jd）。

    淘宝与天猫都归 taobao、抖音与抖店都归 douyin——平台标识是领域概念，
    与商家怎么说无关。
    """
    aliases = {
        "淘宝": "taobao", "天猫": "taobao", "taobao": "taobao",
        "抖店": "douyin", "抖音": "douyin", "douyin": "douyin",
        "京东": "jd", "jd": "jd",
    }
    for key, platform in aliases.items():
        if key in text:
            return platform
    return default


def _extract_target_price(text: str) -> float | None:
    """从用户文本提取目标价（「调到/改为/调整到 99」等形态）。"""
    m = re.search(
        r"(?:改为|调到|调整到|调整为|调整至|改成|改到)\s*(\d+(?:\.\d+)?)", text
    )
    return float(m.group(1)) if m else None


# 读意图词：只用于「没有写意图时」判断这是一次快照查询。
# 注意不要放「改价 / 调价」这类动词——它们由 _PRICE_WRITE_WORDS 认领，
# 否则「改一下价格」会被判成读，实际却是一次未提交的写入（静默丢指令）。
_PRICE_WORDS = ("价格", "定价")
# 写意图词：显式带目标价的写法 + 只有意图没给数值的写法。
# 后者同样按「提交一次改价」处理（目标价用高于成本的兜底值），
# 因为「改一下价格」在语义上就是一次写入请求，不是查询。
_PRICE_WRITE_WORDS = (
    "调到", "改为", "调整到", "调整为", "调整至", "改成", "改到",
    "改价", "调价", "改一下价", "调一下价", "调整价格", "修改价格",
    "改一下价格", "调一下价格", "改下价格", "调下价格",
)


def _slash_skill(user_message: str) -> str | None:
    """提取 "/技能名 ..." 前缀显式指定的技能名（非 / 开头返回 None）。"""
    m = re.match(r"^/([A-Za-z_][A-Za-z0-9_]*)", (user_message or "").strip())
    return m.group(1) if m else None


# 技能名 → 剧本关键词提示：/命令只点名技能时，让 domain 工具分支照常命中。
# 只保留默认技能：扩展技能依赖默认 Agent 未注册的扩展工具，需要扩展通道才可用。
_SKILL_KEYWORD_HINTS = {
    "skill_creator": "创建技能",
    "price_management": "价格",
}


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
        """更新动态配置。

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
                # 第三轮：domain 工具已执行，按结果组织总结并分片流式回放
                reply = _summarize(*_last_tool_context(messages))
                chunks = [c + "。" for c in reply.split("。") if c]
                for i, chunk in enumerate(chunks):
                    yield TextDeltaEvent(text=chunk)
                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.18)
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
        """按关键词映射技能名（与内置技能 keywords 对齐）；/命令显式点名优先。

        默认 Agent 只注册调价闭环工具，因此这里只映射**默认技能**：
        创建技能 → skill_creator；其余需求统一走 price_management
        （它的 SOP 覆盖「查快照 → 提交目标价 → 审批 → 执行 → 回查」）。
        扩展技能（客服/知识库/经营分析等）依赖未注册的扩展工具，剧本不引导。
        """
        explicit = _slash_skill(user_message)
        if explicit:
            return explicit
        if any(w in user_message for w in ("创建技能", "做成", "生成技能", "写个技能", "沉淀")):
            return "skill_creator"
        return "price_management"

    def _domain_tool_start(self, messages: list[dict]) -> ToolStartEvent:
        """第二轮：根据用户消息解析平台 / 商品 / SKU / 目标价，构造工具调用。

        默认调价闭环只暴露 query_product_snapshot 与 update_price 两个业务工具：
        因此这里的主分支是「有目标价 → 提交调价」「否则 → 查快照」，
        平台从消息里识别（淘宝/天猫 → taobao，抖店/抖音 → douyin）。
        旧扩展工具分支保留在下方，供扩展通道与历史评测使用。
        """
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        # 指代继承：「把这个商品调价」这类不带平台/SKU 的指令，从本轮之前的
        # 用户消息里继承最近提到的平台与 SKU（模拟多轮对话的上下文指代）。
        history_user = " ".join(
            str(m.get("content", "")) for m in messages[:-1] if m.get("role") == "user"
        )
        platform = _extract_platform(last_user, default="")
        if not platform:
            platform = _extract_platform(history_user, default="taobao")
        m_sku = re.search(r"SKU-\d+", last_user) or re.search(r"SKU-\d+", history_user)
        sku = m_sku.group(0) if m_sku else "SKU-002"
        m_item = re.search(r"ITEM-\d+", last_user) or re.search(r"ITEM-\d+", history_user)
        product = m_item.group(0) if m_item else "ITEM-1001"
        channel = _extract_channel(last_user, default="")
        if not channel:
            channel = _extract_channel(history_user, default="taobao")
        # /命令只点名技能未带指令时，按技能名补关键词提示，保证分支照常命中
        explicit = _slash_skill(last_user)
        if explicit:
            hint = _SKILL_KEYWORD_HINTS.get(explicit)
            if hint and hint not in last_user:
                last_user = f"{last_user} {hint}"
        if any(w in last_user for w in ("做成", "创建技能", "生成技能", "沉淀")):
            m_name = re.search(r"叫\s*([a-z][a-z0-9_]*)", last_user)
            skill_name = m_name.group(1) if m_name else "created_skill"
            return ToolStartEvent(
                tool_name="save_skill",
                tool_use_id="call_mock_save_skill",
                input={
                    "name": skill_name,
                    "description": "库存查询流程沉淀技能",
                    "keywords": ["库存盘点", "存货核查"],
                    "body": (
                        "执行库存盘点 SOP：\n"
                        "1) 确认平台、商品号与 SKU；\n"
                        "2) 调用 query_product_snapshot 获取当前价与库存；\n"
                        "3) 汇总并向用户汇报；改价前必须先看快照。"
                    ),
                },
            )
        # —— 默认调价闭环：先看目标价，再决定走「快照查询」还是「提交调价」——
        target_price = _extract_target_price(last_user)
        if target_price is not None or any(w in last_user for w in _PRICE_WRITE_WORDS):
            return ToolStartEvent(
                tool_name="update_price",
                tool_use_id="call_mock_price_update",
                input={
                    "platform": _extract_platform(last_user, default=platform),
                    "product_id": product,
                    "sku_id": sku,
                    # 没给目标价时用一个高于成本的默认值，保证剧本可跑通
                    "target_price": target_price if target_price is not None else 99.0,
                },
            )
        if any(w in last_user for w in ("快照", "当前价", "库存", "在售", "在卖")) or any(
            w in last_user for w in _PRICE_WORDS
        ):
            return ToolStartEvent(
                tool_name="query_product_snapshot",
                tool_use_id="call_mock_price_snapshot",
                input={
                    "platform": _extract_platform(last_user, default=platform),
                    "product_id": product,
                    "sku_id": sku,
                },
            )
        if "促销" in last_user or "活动" in last_user:
            return ToolStartEvent(
                tool_name="create_promotion",
                tool_use_id="call_mock_promo",
                input={
                    "channel": channel or "douyin",
                    "sku": sku,
                    "discount": 0.8,
                    "start_time": "2026-09-01 00:00:00",
                    "end_time": "2026-09-07 23:59:59",
                },
            )
        if "价格" in last_user or "调价" in last_user or "改价" in last_user:
            m_price = re.search(r"(?:改为|调到|调整到|调整为|改成)\s*(\d+(?:\.\d+)?)", last_user)
            new_price = float(m_price.group(1)) if m_price else 79.0
            # 成本价不由模型提供——成本保护是平台真相 + 规则引擎的不变量
            return ToolStartEvent(
                tool_name="update_price",
                tool_use_id="call_mock_price",
                input={
                    "channel": channel,
                    "sku": sku,
                    "new_price": new_price,
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
                input={"channel": channel, "period": "近7天"},
            )
        if "售后统计" in last_user or "销售统计" in last_user:
            return ToolStartEvent(
                tool_name="query_after_sales_stats",
                tool_use_id="call_mock_after_sales",
                input={"channel": channel, "period": "近7天"},
            )
        if "异常" in last_user or "预警" in last_user:
            return ToolStartEvent(
                tool_name="query_anomalies",
                tool_use_id="call_mock_anomaly",
                input={"channel": channel},
            )
        if "上架" in last_user or "下架" in last_user or "下柜" in last_user:
            action = "off" if ("下架" in last_user or "下柜" in last_user) else "on"
            return ToolStartEvent(
                tool_name="product_shelf",
                tool_use_id="call_mock_shelf",
                input={"channel": channel, "sku": sku, "action": action},
            )
        if "工单" in last_user or "客诉" in last_user:
            return ToolStartEvent(
                tool_name="service_ticket",
                tool_use_id="call_mock_ticket",
                input={"channel": channel, "order_id": "TB-10086", "issue": "商品破损，申请退货退款", "priority": "high"},
            )
        if "订单" in last_user:
            return ToolStartEvent(
                tool_name="query_order_status",
                tool_use_id="call_mock_order",
                input={"channel": channel, "order_id": "TB-10086"},
            )
        return ToolStartEvent(
            tool_name="query_product_snapshot",
            tool_use_id="call_mock_price_snapshot",
            input={"platform": platform, "product_id": product, "sku_id": sku},
        )
