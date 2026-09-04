"""电商渠道 Source 定义 — 淘宝、京东、抖音。

每个渠道 Source 预置了运营场景所需的工具 Mock 实现。
工具处理函数返回 mock 数据，实际生产环境替换为真实 API 调用。
"""

from sources.source import Source, Tool


def mock_query_inventory(channel: str, sku: str) -> str:
    """Mock 库存查询，按渠道返回不同数据。

    Args:
        channel: 渠道名称（taobao/jd/douyin）。
        sku: 商品 SKU。

    Returns:
        str: Mock 库存数据。
    """
    mock_data = {
        "taobao": {"name": "海洋之风法式泡泡袖连衣裙", "sku": sku, "stock": 1523, "channel": "淘宝"},
        "jd": {"name": "海洋之风高腰 A 字半身裙", "sku": sku, "stock": 890, "channel": "京东", "warehouse": "北京仓"},
        "douyin": {"name": "海洋之风复古针织开衫", "sku": sku, "stock": 2340, "channel": "抖音"},
    }
    result = mock_data.get(channel, {"error": "未知渠道", "channel": channel})
    return str(result)


def mock_update_price(channel: str, sku: str, new_price: float, cost_price: float = 0) -> str:
    """Mock 价格更新。

    Args:
        channel: 渠道名称。
        sku: 商品 SKU。
        new_price: 新价格。
        cost_price: 成本价，用于规则校验。

    Returns:
        str: 更新结果。
    """
    return f"渠道 {channel} 商品 {sku} 价格已更新为 {new_price} 元"


def mock_create_promotion(channel: str, sku: str, discount: float, start_time: str, end_time: str) -> str:
    """Mock 促销创建。

    Args:
        channel: 渠道名称。
        sku: 商品 SKU。
        discount: 折扣率（0.0-1.0）。
        start_time: 开始时间。
        end_time: 结束时间。

    Returns:
        str: 创建结果。
    """
    return f"渠道 {channel} 商品 {sku} 已创建 {discount*100}% 折扣促销，时间: {start_time} ~ {end_time}"


def mock_query_order_status(channel: str, order_id: str) -> str:
    """Mock 订单状态查询。

    Args:
        channel: 渠道名称。
        order_id: 订单 ID。

    Returns:
        str: 订单状态。
    """
    mock_statuses = {
        "taobao": "已发货",
        "jd": "派送中",
        "douyin": "待发货",
    }
    status = mock_statuses.get(channel, "未知")
    return f"渠道 {channel} 订单 {order_id} 状态: {status}"


def create_channel_source(name: str, description: str, source_type: str = "mcp") -> Source:
    """工厂函数：创建指定渠道的 Source。

    Args:
        name: 渠道名称（taobao/jd/douyin）。
        description: 渠道描述。
        source_type: Source 类型（mcp/rest），默认 mcp。

    Returns:
        Source: 配置好的渠道 Source。
    """
    source = Source(name=name, description=description, type=source_type)
    source.add_tool(Tool(
        name="query_inventory",
        description=f"查询{name}渠道商品库存",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": [name]},
                "sku": {"type": "string", "description": "商品 SKU"},
            },
            "required": ["channel", "sku"],
        },
        handler=mock_query_inventory,
    ))
    source.add_tool(Tool(
        name="update_price",
        description=f"更新{name}渠道商品价格",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": [name]},
                "sku": {"type": "string", "description": "商品 SKU"},
                "new_price": {"type": "number", "description": "新价格"},
                "cost_price": {"type": "number", "description": "成本价"},
            },
            "required": ["channel", "sku", "new_price"],
        },
        handler=mock_update_price,
    ))
    source.add_tool(Tool(
        name="create_promotion",
        description=f"在{name}渠道创建促销活动",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": [name]},
                "sku": {"type": "string", "description": "商品 SKU"},
                "discount": {"type": "number", "description": "折扣率 0.0-1.0"},
                "start_time": {"type": "string", "description": "开始时间"},
                "end_time": {"type": "string", "description": "结束时间"},
            },
            "required": ["channel", "sku", "discount", "start_time", "end_time"],
        },
        handler=mock_create_promotion,
    ))
    source.add_tool(Tool(
        name="query_order_status",
        description=f"查询{name}渠道订单状态",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": [name]},
                "order_id": {"type": "string", "description": "订单 ID"},
            },
            "required": ["channel", "order_id"],
        },
        handler=mock_query_order_status,
    ))
    add_batch4_tools(source)
    return source


def create_taobao_source() -> Source:
    """创建淘宝渠道 Source。"""
    return create_channel_source(
        "taobao", "淘宝开放平台 Top API，用于商品管理、订单查询、促销活动"
    )


def create_jd_source() -> Source:
    """创建京东渠道 Source。"""
    return create_channel_source(
        "jd", "京东开放平台 JOS API，用于商品管理、库存查询、促销活动", source_type="rest"
    )


def create_douyin_source() -> Source:
    """创建抖音渠道 Source。"""
    return create_channel_source(
        "douyin", "抖音电商开放平台，用于商品管理、订单查询、直播带货"
    )

# ---------------------------------------------------------------------------
# 批次 4：经营分析 / 商品上下架 / 售后工单 / 经营知识 工具（Mock 实现）
# 分析类与知识库为跨渠道平台级能力；上下架 / 工单按渠道 + 状态维度建模。
# 真实项目：handler 替换为对应平台开放平台 API 调用。
# ---------------------------------------------------------------------------


def mock_product_shelf(channel: str, sku: str, action: str) -> str:
    """Mock 商品上下架。

    Args:
        channel: 渠道名称。
        sku: 商品 SKU。
        action: 动作，on=上架 / off=下架。

    Returns:
        str: 上下架结果。
    """
    action_cn = "上架" if action == "on" else "下架"
    return f"渠道 {channel} 商品 {sku} 已{action_cn}"


def mock_service_ticket(channel: str, order_id: str, issue: str, priority: str = "normal") -> str:
    """Mock 创建售后工单。

    Args:
        channel: 渠道名称。
        order_id: 关联订单号。
        issue: 问题描述。
        priority: 优先级，low / normal / high。

    Returns:
        str: 工单创建结果（含工单号）。
    """
    ticket_id = f"ST-{order_id[-4:]}-{hash(issue) % 1000:03d}"
    return f"已创建售后工单 {ticket_id}（{channel} 订单 {order_id}，优先级 {priority}）：{issue}"


def mock_query_order_stats(channel: str, period: str = "近7天") -> str:
    """Mock 订单/销售分析。

    Args:
        channel: 渠道名称。
        period: 统计周期。

    Returns:
        str: 订单规模、GMV、客单价等指标。
    """
    base = {
        "taobao": {"orders": 1280, "gmv": 85600.0, "avg": 66.9},
        "jd": {"orders": 642, "gmv": 51360.0, "avg": 80.0},
        "douyin": {"orders": 2310, "gmv": 120800.0, "avg": 52.3},
    }
    d = base.get(channel, {"orders": 0, "gmv": 0.0, "avg": 0.0})
    return f"{channel} 渠道 {period} 订单 {d['orders']} 单，GMV {d['gmv']} 元，客单价 {d['avg']} 元"


def mock_query_anomalies(channel: str) -> str:
    """Mock 经营异常排查。

    Args:
        channel: 渠道名称。

    Returns:
        str: 异常项列表（价格、库存、评分维度）。
    """
    cases = {
        "taobao": ["价格低于成本价（SKU-009）", "库存预警（SKU-017 低于安全水位）"],
        "jd": ["无限 SKU 差评集中（SKU-003）"],
        "douyin": [],
    }
    items = cases.get(channel, [])
    return f"{channel} 渠道异常项：{len(items)} 个" + (f"：{'；'.join(items)}" if items else "，经营状态正常")


def mock_query_promotions(channel: str) -> str:
    """Mock 促销活动检查。

    Args:
        channel: 渠道名称。

    Returns:
        str: 进行中/待开始的促销活动列表。
    """
    active = {
        "taobao": [{"name": "双11预热 9折", "discount": 0.9}, {"name": "满300减50", "discount": 0.83}],
        "jd": [{"name": "Plus 会员价 95折", "discount": 0.95}],
        "douyin": [{"name": "直播间秒杀 7折", "discount": 0.7}],
    }
    items = active.get(channel, [])
    names = "、".join(i["name"] for i in items)
    return f"{channel} 渠道进行中的促销：{names if names else '无'}"


def mock_query_after_sales_stats(channel: str, period: str = "近7天") -> str:
    """Mock 售后分析。

    Args:
        channel: 渠道名称。
        period: 统计周期。

    Returns:
        str: 退款率、工单量等指标。
    """
    rates = {
        "taobao": {"refund_rate": 0.021, "tickets": 27},
        "jd": {"refund_rate": 0.015, "tickets": 9},
        "douyin": {"refund_rate": 0.038, "tickets": 88},
    }
    d = rates.get(channel, {"refund_rate": 0.0, "tickets": 0})
    return f"{channel} 渠道 {period} 退款率 {d['refund_rate']*100:.1f}%，售后工单 {d['tickets']} 单"


def mock_query_knowledge_base(topic: str) -> str:
    """Mock 经营知识库查询（平台级 Source，无渠道维度）。

    Args:
        topic: 知识主题（话术/规范/政策等）。

    Returns:
        str: 匹配的经营知识条目。
    """
    knowledge = {
        "话术": "售后安抚话术：先致歉共情，再给方案与时效承诺，最后回访闭环。",
        "上架规范": "上架前核对主图 5 张、详情页合规、价格不低于成本预警线。",
        "退款政策": "仅退款适用于未发货订单；已发货走退货退款流程，48 小时内处理。",
        "发货时效": "现货 24 小时内发货，预售按页面承诺时效，超时自动赔付。",
    }
    for key, text in knowledge.items():
        if key in topic:
            return f"[知识库] {key}：{text}"
    return f"[知识库] 未找到“{topic}”相关条目，可补充完善知识库文档"


def add_batch4_tools(source: Source) -> None:
    """给渠道 Source 追加批次 4 工具（经营分析 / 上下架 / 工单 / 知识库）。

    Args:
        source: 目标 Source，工具以 name 追加，不覆盖已有同名工具（幂等）。
    """
    existing = {t.name for t in source.tools}

    def _add(tool: Tool) -> None:
        if tool.name not in existing:
            source.add_tool(tool)

    _add(Tool(
        name="product_shelf",
        description="商品上下架操作",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["taobao", "jd", "douyin"]},
                "sku": {"type": "string", "description": "商品 SKU"},
                "action": {"type": "string", "enum": ["on", "off"], "description": "on=上架 / off=下架"},
            },
            "required": ["channel", "sku", "action"],
        },
        handler=mock_product_shelf,
    ))

    _add(Tool(
        name="service_ticket",
        description="创建售后工单",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["taobao", "jd", "douyin"]},
                "order_id": {"type": "string", "description": "关联订单号"},
                "issue": {"type": "string", "description": "问题描述"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"], "description": "优先级"},
            },
            "required": ["channel", "order_id", "issue"],
        },
        handler=mock_service_ticket,
    ))

    _add(Tool(
        name="query_order_stats",
        description="订单/销售分析（订单量、GMV、客单价）",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["taobao", "jd", "douyin"]},
                "period": {"type": "string", "description": "统计周期"},
            },
            "required": ["channel"],
        },
        handler=mock_query_order_stats,
    ))

    _add(Tool(
        name="query_anomalies",
        description="经营异常排查（价格/库存/评分）",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["taobao", "jd", "douyin"]},
            },
            "required": ["channel"],
        },
        handler=mock_query_anomalies,
    ))

    _add(Tool(
        name="query_promotions",
        description="促销活动检查（进行中活动）",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["taobao", "jd", "douyin"]},
            },
            "required": ["channel"],
        },
        handler=mock_query_promotions,
    ))

    _add(Tool(
        name="query_after_sales_stats",
        description="售后分析（退款率、工单量）",
        parameters={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["taobao", "jd", "douyin"]},
                "period": {"type": "string", "description": "统计周期"},
            },
            "required": ["channel"],
        },
        handler=mock_query_after_sales_stats,
    ))

    _add(Tool(
        name="query_knowledge_base",
        description="经营知识库查询（话术/政策/规范）",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "知识主题"},
            },
            "required": ["topic"],
        },
        handler=mock_query_knowledge_base,
    ))