"""批次 4 测试 — 经营分析 / 上下架 / 售后工单 / 知识库工具、技能注册、RBAC ACL 扩展。"""

import asyncio

from mocks.channel_sources import (
    add_batch4_tools,
    mock_product_shelf,
    mock_service_ticket,
    mock_query_order_stats,
    mock_query_anomalies,
    mock_query_promotions,
    mock_query_after_sales_stats,
    mock_query_knowledge_base,
    create_taobao_source,
    create_jd_source,
    create_douyin_source,
)
from sources.mcp_client_pool import McpClientPool
from sources.skill_registry import create_default_registry
from permission.rbac import can_role_write, role_gate_rule
from permission.pre_tool_use import PreToolUseAction

BATCH4_TOOLS = {
    "product_shelf", "service_ticket", "query_order_stats", "query_anomalies",
    "query_promotions", "query_after_sales_stats", "query_knowledge_base",
}


def test_mock_handlers_output_structure():
    """7 个新 handler 返回结果包含关键字段。"""
    assert "已上架" in mock_product_shelf("taobao", "SKU-001", "on")
    assert "已下架" in mock_product_shelf("jd", "SKU-001", "off")
    assert "售后工单" in mock_service_ticket("taobao", "TB-10086", "商品破损")
    assert "GMV" in mock_query_order_stats("taobao", "近7天")
    assert "异常" in mock_query_anomalies("taobao")
    assert "促销" in mock_query_promotions("douyin")
    assert "退款率" in mock_query_after_sales_stats("taobao")
    assert "知识库" in mock_query_knowledge_base("退款政策")
    assert "未找到" in mock_query_knowledge_base("不存在的主题")


def test_all_sources_expose_batch4_tools():
    """三个渠道 Source 均暴露 11 个工具，含全部批次 4 工具。"""
    for factory in (create_taobao_source, create_jd_source, create_douyin_source):
        src = factory()
        names = {t["name"] for t in src.get_tool_definitions()}
        assert BATCH4_TOOLS <= names, factory.__name__
        assert len(names) == 11


def test_add_batch4_tools_idempotent():
    """重复追加不产生重复工具名。"""
    src = create_taobao_source()
    before = [t.name for t in src.tools]
    add_batch4_tools(src)
    after = [t.name for t in src.tools]
    assert before == after


def test_skill_registry_batch4():
    """技能注册表补全：product_listing / after_sales 挂工具，新增 4 技能。"""
    reg = create_default_registry()
    listing = reg.get("product_listing")
    assert listing is not None and "product_shelf" in listing.tools
    after = reg.get("after_sales")
    assert after is not None and {"service_ticket", "query_after_sales_stats"} <= set(after.tools)
    for name in ("order_analytics", "anomaly_detection", "knowledge_inquiry"):
        skill = reg.get(name)
        assert skill is not None and skill.tools, name
    # 关键词解析命中新技能
    assert reg.resolve("查一下淘宝的销售分析")[0].name == "order_analytics"
    assert {s.name for s in reg.resolve("客服话术有哪些")} == {"knowledge_inquiry"}
    assert "product_shelf" in {t for s in reg.resolve("把SKU-001下架") for t in s.tools}


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


def _run(coro):
    return asyncio.run(coro)


def test_mcp_discover_11_tools_and_call():
    """真实 MCP 协议下发现 11 个工具并可调用批次 4 工具。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            names = {d["name"] for d in pool.get_all_tool_definitions()}
            assert BATCH4_TOOLS <= names
            r = await pool.call_tool("product_shelf", {"channel": "taobao", "sku": "SKU-001", "action": "off"})
            assert "已下架" in r
            r2 = await pool.call_tool("query_order_stats", {"channel": "jd"})
            assert "GMV" in r2
            r3 = await pool.call_tool("query_knowledge_base", {"topic": "退款政策"})
            assert "知识库" in r3
        finally:
            await pool.close()

    _run(scenario())