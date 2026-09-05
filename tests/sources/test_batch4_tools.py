"""批次 4 测试 — 内置平台 API 工具（builtin_tools）、技能注册、RBAC ACL 扩展。"""

import asyncio

from sources import builtin_tools
from sources.mcp_client_pool import McpClientPool
from sources.skill_registry import create_default_registry
from permission.rbac import can_role_write, role_gate_rule
from permission.pre_tool_use import PreToolUseAction

BATCH4_TOOLS = {
    "product_shelf", "service_ticket", "query_order_stats", "query_anomalies",
    "query_promotions", "query_after_sales_stats", "query_knowledge_base",
}

ALL_11_TOOLS = {
    "query_inventory", "update_price", "create_promotion", "query_order_status",
    "product_shelf", "service_ticket", "query_order_stats", "query_anomalies",
    "query_promotions", "query_after_sales_stats", "query_knowledge_base",
}


def _run(coro):
    return asyncio.run(coro)


def test_builtin_registry_exposes_11_tools():
    """内置注册表暴露全部 11 个电商语义操作 + save_skill，定义含 JSON Schema。"""
    names = set(builtin_tools.tool_names())
    assert ALL_11_TOOLS <= names
    assert "save_skill" in names
    assert BATCH4_TOOLS <= names
    for d in builtin_tools.get_definitions():
        assert d["parameters"]["type"] == "object", d["name"]


def test_builtin_handlers_output_structure():
    """7 个批次 4 handler 经 REST→Adapter→mock 网关链路返回关键文本。"""
    h = builtin_tools.get_handlers()
    assert "已上架" in _run(h["product_shelf"](channel="taobao", sku="SKU-001", action="on"))
    assert "已下架" in _run(h["product_shelf"](channel="jd", sku="SKU-001", action="off"))
    assert "售后工单" in _run(h["service_ticket"](channel="taobao", order_id="TB-10086", issue="商品破损"))
    assert "GMV" in _run(h["query_order_stats"](channel="taobao", period="近7天"))
    assert "异常" in _run(h["query_anomalies"](channel="taobao"))
    assert "促销" in _run(h["query_promotions"](channel="douyin"))
    assert "退款率" in _run(h["query_after_sales_stats"](channel="taobao"))
    assert "知识库" in _run(h["query_knowledge_base"](topic="退款政策"))
    assert "未找到" in _run(h["query_knowledge_base"](topic="不存在的主题"))


def test_builtin_handler_platform_error_text():
    """未知渠道 → 平台错误文本（不抛异常，LLM 可解释）。"""
    r = _run(builtin_tools.query_inventory(channel="nope", sku="SKU-1"))
    assert "[平台错误" in r and "nope" in r


def test_skill_registry_batch4():
    """技能注册表补全：product_listing / after_sales 的 SOP 正文覆盖工具，新增 4 技能。"""
    reg = create_default_registry()
    listing = reg.get("product_listing")
    assert listing is not None and "product_shelf" in listing.body
    after = reg.get("after_sales")
    assert after is not None and "service_ticket" in after.body and "query_order_status" in after.body
    for name in ("order_analytics", "anomaly_detection", "knowledge_inquiry"):
        skill = reg.get(name)
        assert skill is not None and skill.body, name
    # 关键词解析命中新技能
    assert reg.resolve("查一下淘宝的销售分析")[0].name == "order_analytics"
    assert {s.name for s in reg.resolve("客服话术有哪些")} == {"knowledge_inquiry"}
    assert "product_shelf" in reg.resolve("把SKU-001下架")[0].body


def test_rbac_write_acl_expansion():
    """ACL 扩展：上下架归店长/运营，售后工单归店长/客服。"""
    assert can_role_write("manager", "product_shelf")
    assert can_role_write("operator", "product_shelf")
    assert not can_role_write("customer_service", "product_shelf")
    assert not can_role_write("finance", "product_shelf")

    assert can_role_write("manager", "service_ticket")
    assert can_role_write("customer_service", "service_ticket")
    assert not can_role_write("operator", "service_ticket")
    assert not can_role_write("finance", "service_ticket")

    # 店长全权四类写操作
    for tool in ("update_price", "create_promotion", "product_shelf", "service_ticket"):
        assert can_role_write("manager", tool)


def test_rbac_read_tools_open_to_all():
    """分析/排查/知识库读工具对所有角色开放（query_ 前缀）。"""
    reads = ["query_order_stats", "query_anomalies", "query_promotions",
             "query_after_sales_stats", "query_knowledge_base"]
    for role in ("manager", "operator", "customer_service", "finance"):
        for tool in reads:
            assert can_role_write(role, tool), (role, tool)
        gate = role_gate_rule(role)
        for tool in reads:
            assert gate(tool, {}).action == PreToolUseAction.ALLOW


def test_rbac_gate_batch4_role_separation():
    """身份门实测：客服可开工单但不可上下架；运营可上下架但不可开工单。"""
    cs_gate = role_gate_rule("customer_service")
    assert cs_gate("service_ticket", {"order_id": "TB-1"}).action == PreToolUseAction.ALLOW
    blocked = cs_gate("product_shelf", {"action": "on"})
    assert blocked.action == PreToolUseAction.BLOCK and "无权" in blocked.reason

    op_gate = role_gate_rule("operator")
    assert op_gate("product_shelf", {"action": "on"}).action == PreToolUseAction.ALLOW
    assert op_gate("service_ticket", {}).action == PreToolUseAction.BLOCK


def test_mcp_server_exposes_demo_external_tools():
    """MCP 服务重定位为外部工具模拟：真实协议发现并调用演示工具。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            names = {d["name"] for d in pool.get_all_tool_definitions()}
            assert {"query_exchange_rate", "query_weather"} <= names
            assert "update_price" not in names  # 电商工具已迁内置通道
            r = await pool.call_tool("query_exchange_rate", {"currency": "USD"})
            assert "7.18" in r
            r2 = await pool.call_tool("query_weather", {"city": "杭州"})
            assert "晴" in r2
        finally:
            await pool.close()

    _run(scenario())