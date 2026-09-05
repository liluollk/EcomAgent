"""内置平台 API 工具 — 11 个电商运营语义操作（工具三通道之一）。

定位（工具三通道）：
  1. 内置平台 API 工具（本模块）：电商运营语义操作，handler 经
     ChannelRestClient → PlatformAdapter 打到渠道平台（当前 mock 网关，
     真实平台资质到位后换 adapter 实现，工具名与文本模板不变）；
  2. MCP 外部工具：用户经 mcp_servers.json 配置接入的第三方工具服务；
  3. 专用工具（如 save_skill）：见 builtin_tools 扩展。

与 mocks/mcp_tool_server.py 的旧实现逐字对齐（参数名/默认值/结果文本），
保证 e2e/harness 断言与前端展示零漂移。handler 契约：**kwargs → str。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from sources.rest_client import RestApiError, get_rest_client

_ACTION_CN = {"on": "上架", "off": "下架"}


async def _exec(channel, operation: str, params: dict) -> dict:
    """执行一次语义操作：走平台适配器翻译请求 + 归一化响应。

    PlatformAdapter（按渠道 platform 字段选择）负责：
      - build_request(operation, channel, params) -> (method, path, http_kwargs)
      - parse_response(data) -> 归一化 dict（字段名与 mock 约定一致）
    channel 为 None 时（平台级操作）使用 mock 适配器与默认客户端。
    """
    from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

    adapter = DEFAULT_CHANNEL_REGISTRY.executor_for(channel)
    if channel is not None:
        client = DEFAULT_CHANNEL_REGISTRY.client_for(channel)
        if client is None:
            return {"_platform_error": f"[平台错误 10002] 渠道不存在: {channel}"}
    else:
        client = get_rest_client()
    try:
        method, path, http_kwargs = adapter.build_request(operation, channel, params)
        data = await client.call(method, path, **http_kwargs)
        return adapter.parse_response(data)
    except RestApiError as e:
        # 返回平台错误文本（而非抛异常），让上层 LLM 可理解可解释
        return {"_platform_error": f"[平台错误 {e.code}] {e.message}"}


# ----------------------------------------------------------------------
# 11 个语义操作 handler
# ----------------------------------------------------------------------

async def query_inventory(channel: str, sku: str) -> str:
    data = await _exec(channel, "query_inventory", {"sku": sku})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 商品 {sku} 库存 {data.get('stock', 0)} 件：{data.get('name', '')}"


async def update_price(channel: str, sku: str, new_price: float, cost_price: float = 0) -> str:
    data = await _exec(channel, "update_price", {"sku": sku, "new_price": new_price, "cost_price": cost_price})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 商品 {sku} 价格已更新为 {new_price} 元"


async def create_promotion(channel: str, sku: str, discount: float, start_time: str, end_time: str) -> str:
    data = await _exec(channel, "create_promotion", {"sku": sku, "discount": discount, "start_time": start_time, "end_time": end_time})
    if "_platform_error" in data:
        return data["_platform_error"]
    replay = "（幂等：重复请求，未重复创建）" if data.get("idempotent_replay") else ""
    return f"渠道 {channel} 商品 {sku} 已创建 {discount*100}% 折扣促销，时间: {start_time} ~ {end_time}{replay}"


async def query_order_status(channel: str, order_id: str) -> str:
    data = await _exec(channel, "query_order_status", {"order_id": order_id})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 订单 {order_id} 状态: {data.get('status', '未知')}"


async def product_shelf(channel: str, sku: str, action: str) -> str:
    data = await _exec(channel, "product_shelf", {"sku": sku, "action": action})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 商品 {sku} 已{_ACTION_CN.get(action, action)}"


async def service_ticket(channel: str, order_id: str, issue: str, priority: str = "normal") -> str:
    data = await _exec(channel, "service_ticket", {"order_id": order_id, "issue": issue, "priority": priority})
    if "_platform_error" in data:
        return data["_platform_error"]
    replay = "（幂等：重复请求，未重复创建）" if data.get("idempotent_replay") else ""
    return (f"已创建售后工单 {data.get('ticket_id', '')}"
            f"（{channel} 订单 {order_id}，优先级 {priority}）：{issue}{replay}")


async def query_order_stats(channel: str, period: str = "近7天") -> str:
    data = await _exec(channel, "query_order_stats", {"period": period})
    if "_platform_error" in data:
        return data["_platform_error"]
    return (f"{channel} 渠道 {period} 订单 {data.get('orders', 0)} 单，"
            f"GMV {data.get('gmv', 0)} 元，客单价 {data.get('avg', 0)} 元")


async def query_anomalies(channel: str) -> str:
    data = await _exec(channel, "query_anomalies", {})
    if "_platform_error" in data:
        return data["_platform_error"]
    items = data.get("items", [])
    detail = "：" + "；".join(items) if items else "，经营状态正常"
    return f"{channel} 渠道异常项：{len(items)} 个{detail}"


async def query_promotions(channel: str) -> str:
    data = await _exec(channel, "query_promotions", {})
    if "_platform_error" in data:
        return data["_platform_error"]
    items = data.get("items", [])
    names = "、".join(i.get("name", "") for i in items)
    return f"{channel} 渠道进行中的促销：{names if names else '无'}"


async def query_after_sales_stats(channel: str, period: str = "近7天") -> str:
    data = await _exec(channel, "query_after_sales_stats", {"period": period})
    if "_platform_error" in data:
        return data["_platform_error"]
    return (f"{channel} 渠道 {period} 退款率 {data.get('refund_rate', 0) * 100:.1f}%，"
            f"售后工单 {data.get('tickets', 0)} 单")


async def query_knowledge_base(topic: str) -> str:
    data = await _exec(None, "query_knowledge_base", {"topic": topic})
    if "_platform_error" in data:
        return data["_platform_error"]
    if data.get("matched"):
        return f"[知识库] {data.get('key', '')}：{data.get('text', '')}"
    return f"[知识库] 未找到「{topic}」相关条目，可补充完善知识库文档"


async def save_skill(name: str, description: str, body: str, keywords: Optional[list] = None) -> str:
    """保存新的运营技能（SKILL.md 知识包）——skill-creator 元技能的落盘工具。

    校验 name/description/body → 经技能注册表写入用户技能目录（data/skills/），
    mtime 热加载即时生效；失败返回可解释错误文本（不抛异常）。
    """
    import re

    from sources.skill_registry import DEFAULT_SKILL_REGISTRY, Skill

    name = (name or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name):
        return (
            f"[保存失败] 技能标识 {name!r} 不合法：需小写字母开头，"
            "仅含小写字母 / 数字 / 下划线，长度 2-41"
        )
    if not (description or "").strip():
        return "[保存失败] description 必填（用于技能菜单展示与意图识别）"
    if not (body or "").strip():
        return "[保存失败] SOP 正文必填（操作手册 / 平台规则内容）"
    try:
        DEFAULT_SKILL_REGISTRY.add(Skill(
            name=name,
            description=description.strip(),
            keywords=[str(k) for k in (keywords or [])],
            body=body.strip(),
        ))
    except ValueError as exc:
        return f"[保存失败] {exc}"
    return (
        f"技能 {name} 已创建（SKILL.md 已持久化，热加载即时生效）。"
        f"当前技能菜单：{'、'.join(DEFAULT_SKILL_REGISTRY.list_menu_names())}"
    )


# ----------------------------------------------------------------------
# 工具定义（JSON Schema 与 handler 签名逐字对齐）
# ----------------------------------------------------------------------

_CHANNEL = {"type": "string", "description": "渠道标识，可选 taobao / jd / douyin。"}
_SKU = {"type": "string", "description": "商品 SKU。"}
_PERIOD = {"type": "string", "description": "统计周期。", "default": "近7天"}

_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "query_inventory",
        "description": "查询指定渠道商品库存。",
        "parameters": {
            "type": "object",
            "properties": {"channel": _CHANNEL, "sku": _SKU},
            "required": ["channel", "sku"],
        },
    },
    {
        "name": "update_price",
        "description": "更新指定渠道商品价格。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "sku": _SKU,
                "new_price": {"type": "number", "description": "新价格。"},
                "cost_price": {"type": "number", "description": "成本价，用于规则校验。", "default": 0},
            },
            "required": ["channel", "sku", "new_price"],
        },
    },
    {
        "name": "create_promotion",
        "description": "在指定渠道创建促销活动。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "sku": _SKU,
                "discount": {"type": "number", "description": "折扣率（0.0-1.0）。"},
                "start_time": {"type": "string", "description": "开始时间。"},
                "end_time": {"type": "string", "description": "结束时间。"},
            },
            "required": ["channel", "sku", "discount", "start_time", "end_time"],
        },
    },
    {
        "name": "query_order_status",
        "description": "查询指定渠道订单的当前状态。",
        "parameters": {
            "type": "object",
            "properties": {"channel": _CHANNEL, "order_id": {"type": "string", "description": "订单号。"}},
            "required": ["channel", "order_id"],
        },
    },
    {
        "name": "product_shelf",
        "description": "商品上下架操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "sku": _SKU,
                "action": {"type": "string", "description": "on=上架 / off=下架。"},
            },
            "required": ["channel", "sku", "action"],
        },
    },
    {
        "name": "service_ticket",
        "description": "创建售后工单。",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": _CHANNEL,
                "order_id": {"type": "string", "description": "关联订单号。"},
                "issue": {"type": "string", "description": "问题描述。"},
                "priority": {"type": "string", "description": "优先级，low / normal / high。", "default": "normal"},
            },
            "required": ["channel", "order_id", "issue"],
        },
    },
    {
        "name": "query_order_stats",
        "description": "订单/销售分析（订单量、GMV、客单价）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": _CHANNEL, "period": _PERIOD},
            "required": ["channel"],
        },
    },
    {
        "name": "query_anomalies",
        "description": "经营异常排查（价格/库存/评分）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": _CHANNEL},
            "required": ["channel"],
        },
    },
    {
        "name": "query_promotions",
        "description": "促销活动检查（进行中活动）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": _CHANNEL},
            "required": ["channel"],
        },
    },
    {
        "name": "query_after_sales_stats",
        "description": "售后分析（退款率、工单量）。",
        "parameters": {
            "type": "object",
            "properties": {"channel": _CHANNEL, "period": _PERIOD},
            "required": ["channel"],
        },
    },
    {
        "name": "query_knowledge_base",
        "description": "经营知识库查询（话术/政策/规范）。",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "知识主题，如\"退款政策\"\"上架规范\"。"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "save_skill",
        "description": (
            "保存一个新的运营技能（SKILL.md 知识包）：name 为小写下划线标识，"
            "description 一句话描述，keywords 触发关键词，body 为 SOP 操作手册 / "
            "平台规则正文。写操作，需权限确认后落盘。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能标识（小写字母开头，可含数字/下划线）。"},
                "description": {"type": "string", "description": "一句话技能描述。"},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "触发关键词列表。",
                },
                "body": {"type": "string", "description": "SOP 正文（markdown）。"},
            },
            "required": ["name", "description", "body"],
        },
    },
]

_HANDLERS: dict[str, Callable[..., Any]] = {
    "query_inventory": query_inventory,
    "update_price": update_price,
    "create_promotion": create_promotion,
    "query_order_status": query_order_status,
    "product_shelf": product_shelf,
    "service_ticket": service_ticket,
    "query_order_stats": query_order_stats,
    "query_anomalies": query_anomalies,
    "query_promotions": query_promotions,
    "query_after_sales_stats": query_after_sales_stats,
    "query_knowledge_base": query_knowledge_base,
    "save_skill": save_skill,
}


def get_definitions() -> list[dict[str, Any]]:
    """内置工具定义列表（OpenAI function-calling 形状）。"""
    return [dict(d) for d in _DEFINITIONS]


def get_handlers() -> dict[str, Callable[..., Any]]:
    """内置工具 handler 映射（**kwargs → str，async 安全）。"""
    return dict(_HANDLERS)


def tool_names() -> list[str]:
    """内置工具名列表（测试与文档用）。"""
    return [d["name"] for d in _DEFINITIONS]