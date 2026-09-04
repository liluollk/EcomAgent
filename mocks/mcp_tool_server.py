"""
真实 MCP Server — 将电商渠道业务工具通过标准 MCP 协议暴露，
工具执行走真实 HTTP/REST 调用 mock 渠道平台服务。

分层（三层协议/数据）：
  1. MCP 协议层（真实）：FastMCP + stdio 传输，JSON-RPC 初始化/发现/调用。
  2. REST 协议层（真实）：@mcp.tool 内部经 ChannelRestClient 发 HTTP 请求到
     mock 平台服务（mocks/channel_api_mock.py，独立进程）。
  3. 业务数据层（Mock）：平台返回的库存/价格/订单等为模拟数据——真实平台
     数据需平台资质与 OAuth 鉴权。

换真实渠道：替换平台服务地址（CHANNEL_API_URL / base_url）与鉴权实现，
本 Server 的工具定义、参数、错误文本语义不变。

运行方式（被 McpClientPool 以子进程拉起）:
    python -m mocks.mcp_tool_server
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from sources.rest_client import RestApiError, get_rest_client

# 服务名：MCP initialize 握手时返回给客户端
SERVER_NAME = "ecommerce-channel-tools"
SERVER_VERSION = "1.1.0"

mcp = FastMCP(SERVER_NAME)

_ACTION_CN = {"on": "上架", "off": "下架"}


async def _exec(channel, operation: str, params: dict) -> dict:
    """执行一次语义操作：走平台适配器翻译请求 + 归一化响应。

    与旧 _rest(channel, method, path) 的区别：工具层不再自己拼 method/path/字段，
    由 PlatformAdapter（按渠道 platform 字段选择）负责：
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


@mcp.tool()
async def query_inventory(channel: str, sku: str) -> str:
    """查询指定渠道商品库存。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        sku: 商品 SKU。

    Returns:
        str: 库存数据文本。
    """
    data = await _exec(channel, "query_inventory", {"sku": sku})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 商品 {sku} 库存 {data.get('stock', 0)} 件：{data.get('name', '')}"


@mcp.tool()
async def update_price(channel: str, sku: str, new_price: float, cost_price: float = 0) -> str:
    """更新指定渠道商品价格。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        sku: 商品 SKU。
        new_price: 新价格。
        cost_price: 成本价，用于规则校验。

    Returns:
        str: 更新结果文本。
    """
    data = await _exec(channel, "update_price", {"sku": sku, "new_price": new_price, "cost_price": cost_price})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 商品 {sku} 价格已更新为 {new_price} 元"


@mcp.tool()
async def create_promotion(channel: str, sku: str, discount: float, start_time: str, end_time: str) -> str:
    """在指定渠道创建促销活动。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        sku: 商品 SKU。
        discount: 折扣率（0.0-1.0）。
        start_time: 开始时间。
        end_time: 结束时间。

    Returns:
        str: 创建结果文本。
    """
    data = await _exec(channel, "create_promotion", {"sku": sku, "discount": discount, "start_time": start_time, "end_time": end_time})
    if "_platform_error" in data:
        return data["_platform_error"]
    replay = "（幂等：重复请求，未重复创建）" if data.get("idempotent_replay") else ""
    return f"渠道 {channel} 商品 {sku} 已创建 {discount*100}% 折扣促销，时间: {start_time} ~ {end_time}{replay}"


@mcp.tool()
async def query_order_status(channel: str, order_id: str) -> str:
    """查询指定渠道订单的当前状态。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        order_id: 订单号。

    Returns:
        str: 订单状态文本。
    """
    data = await _exec(channel, "query_order_status", {"order_id": order_id})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 订单 {order_id} 状态: {data.get('status', '未知')}"


@mcp.tool()
async def product_shelf(channel: str, sku: str, action: str) -> str:
    """商品上下架操作。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        sku: 商品 SKU。
        action: on=上架 / off=下架。

    Returns:
        str: 上下架结果。
    """
    data = await _exec(channel, "product_shelf", {"sku": sku, "action": action})
    if "_platform_error" in data:
        return data["_platform_error"]
    return f"渠道 {channel} 商品 {sku} 已{_ACTION_CN.get(action, action)}"


@mcp.tool()
async def service_ticket(channel: str, order_id: str, issue: str, priority: str = "normal") -> str:
    """创建售后工单。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        order_id: 关联订单号。
        issue: 问题描述。
        priority: 优先级，low / normal / high。

    Returns:
        str: 工单创建结果。
    """
    data = await _exec(channel, "service_ticket", {"order_id": order_id, "issue": issue, "priority": priority})
    if "_platform_error" in data:
        return data["_platform_error"]
    replay = "（幂等：重复请求，未重复创建）" if data.get("idempotent_replay") else ""
    return (f"已创建售后工单 {data.get('ticket_id', '')}"
            f"（{channel} 订单 {order_id}，优先级 {priority}）：{issue}{replay}")


@mcp.tool()
async def query_order_stats(channel: str, period: str = "近7天") -> str:
    """订单/销售分析（订单量、GMV、客单价）。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        period: 统计周期。

    Returns:
        str: 分析文本。
    """
    data = await _exec(channel, "query_order_stats", {"period": period})
    if "_platform_error" in data:
        return data["_platform_error"]
    return (f"{channel} 渠道 {period} 订单 {data.get('orders', 0)} 单，"
            f"GMV {data.get('gmv', 0)} 元，客单价 {data.get('avg', 0)} 元")


@mcp.tool()
async def query_anomalies(channel: str) -> str:
    """经营异常排查（价格/库存/评分）。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。

    Returns:
        str: 异常项列表文本。
    """
    data = await _exec(channel, "query_anomalies", {})
    if "_platform_error" in data:
        return data["_platform_error"]
    items = data.get("items", [])
    detail = "：" + "；".join(items) if items else "，经营状态正常"
    return f"{channel} 渠道异常项：{len(items)} 个{detail}"


@mcp.tool()
async def query_promotions(channel: str) -> str:
    """促销活动检查（进行中活动）。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。

    Returns:
        str: 活动列表文本。
    """
    data = await _exec(channel, "query_promotions", {})
    if "_platform_error" in data:
        return data["_platform_error"]
    items = data.get("items", [])
    names = "、".join(i.get("name", "") for i in items)
    return f"{channel} 渠道进行中的促销：{names if names else '无'}"


@mcp.tool()
async def query_after_sales_stats(channel: str, period: str = "近7天") -> str:
    """售后分析（退款率、工单量）。

    Args:
        channel: 渠道标识，可选 taobao / jd / douyin。
        period: 统计周期。

    Returns:
        str: 售后指标文本。
    """
    data = await _exec(channel, "query_after_sales_stats", {"period": period})
    if "_platform_error" in data:
        return data["_platform_error"]
    return (f"{channel} 渠道 {period} 退款率 {data.get('refund_rate', 0) * 100:.1f}%，"
            f"售后工单 {data.get('tickets', 0)} 单")


@mcp.tool()
async def query_knowledge_base(topic: str) -> str:
    """经营知识库查询（话术/政策/规范）。

    Args:
        topic: 知识主题，如"退款政策""上架规范"。

    Returns:
        str: 匹配的知识条目。
    """
    data = await _exec(None, "query_knowledge_base", {"topic": topic})
    if "_platform_error" in data:
        return data["_platform_error"]
    if data.get("matched"):
        return f"[知识库] {data.get('key', '')}：{data.get('text', '')}"
    return f"[知识库] 未找到「{topic}」相关条目，可补充完善知识库文档"


if __name__ == "__main__":
    # stdio 传输启动：供 McpClientPool 以子进程方式拉起并连接
    mcp.run()