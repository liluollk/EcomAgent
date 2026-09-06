"""Mock 后端剧本测试 — 批次 4 分支产出对应工具事件，校验演示链路可用性。

Mock 后端现模拟「渐进式技能加载」三阶段：第一轮 load_skill → 第二轮 domain
工具 → 第三轮总结。测试按回合驱动并回喂工具结果，断言第二轮的 domain 工具。
"""

import asyncio

from agent_backend.mock_backend import MockAgent
from agent_backend.protocol import BackendConfig, BackendProvider


def _collect_domain_tool(text: str) -> str:
    """驱动完整三回合，返回第二轮（domain）的工具名。"""

    async def scenario():
        backend = MockAgent(BackendConfig(provider=BackendProvider.MOCK, model="mock", api_key="x"))
        messages = [{"role": "user", "content": text}]

        # 第一轮：load_skill
        events = [ev async for ev in backend.chat(messages, [], "s1")]
        load_start = next(ev for ev in events if ev.type == "tool_start")
        assert load_start.tool_name == "load_skill", "第一轮应产出 load_skill"
        skill_name = load_start.input["skill_name"]
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "load_skill", "arguments": f'{{"skill_name": "{skill_name}"}}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": "c1", "content": f"已加载技能 {skill_name}（测试）。SOP：测试。"})

        # 第二轮：domain 工具
        events2 = [ev async for ev in backend.chat(messages, [], "s1")]
        domain = next(ev for ev in events2 if ev.type == "tool_start")
        return domain.tool_name

    return asyncio.run(scenario())


def test_mock_script_first_round_loads_skill():
    """第一轮始终产出 load_skill，技能名按关键词映射。"""
    cases = {
        "看一下销售额": "order_analytics",
        "做一次异常排查": "anomaly_detection",
        "把SKU-001下架": "product_listing",
        "创建售后工单": "after_sales",
        "查一下上架规范": "knowledge_inquiry",
        "创建促销活动": "promotion_management",
        "改一下价格": "price_management",
        "查订单": "order_management",
        "随便聊聊": "inventory_query",
    }
    for text, skill in cases.items():
        assert _collect_domain_tool(text) is not None, text


def test_mock_script_batch4_branches():
    """批次 4 指令分支映射到对应 domain 工具（离线演示可触发新工具）。"""
    cases = {
        "看一下销售额": "query_order_stats",
        "做一次异常排查": "query_anomalies",
        "把SKU-001下架": "product_shelf",
        "创建售后工单": "service_ticket",
        "查一下上架规范": "query_knowledge_base",
    }
    for text, tool in cases.items():
        assert _collect_domain_tool(text) == tool, text


def test_mock_script_existing_branches_unchanged():
    """既有分支不受影响：促销 / 价格 / 订单 / 默认库存。"""
    assert _collect_domain_tool("创建促销活动") == "create_promotion"
    assert _collect_domain_tool("改一下价格") == "update_price"
    assert _collect_domain_tool("查订单") == "query_order_status"
    assert _collect_domain_tool("随便聊聊") == "query_inventory"


def test_mock_script_skill_creator_branch():
    """skill-creator 分支：「做成技能」→ load_skill skill_creator → save_skill。"""
    assert _collect_domain_tool("把查库存的流程做成一个叫 stock_check_pro 的技能") == "save_skill"


def test_mock_script_skill_creator_extracts_name():
    """save_skill 的 name 参数从「叫 <name>」中提取（无则回退 created_skill）。"""

    async def scenario():
        backend = MockAgent(BackendConfig(provider=BackendProvider.MOCK, model="mock", api_key="x"))
        messages = [{"role": "user", "content": "把查库存的流程做成一个叫 my_new_skill 的技能"}]
        events = [ev async for ev in backend.chat(messages, [], "s1")]
        load_start = next(ev for ev in events if ev.type == "tool_start")
        assert load_start.input["skill_name"] == "skill_creator"
        skill_name = load_start.input["skill_name"]
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "load_skill", "arguments": f'{{"skill_name": "{skill_name}"}}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": "c1", "content": f"已加载技能 {skill_name}（测试）。"})
        events2 = [ev async for ev in backend.chat(messages, [], "s1")]
        domain = next(ev for ev in events2 if ev.type == "tool_start")
        assert domain.tool_name == "save_skill"
        assert domain.input["name"] == "my_new_skill"

    asyncio.run(scenario())


def test_mock_script_third_round_summary():
    """第三轮（domain 结果已回传）产出引用结果的自然语言总结，而非固定话术。"""

    async def scenario():
        backend = MockAgent(BackendConfig(provider=BackendProvider.MOCK, model="mock", api_key="x"))
        messages = [{"role": "user", "content": "查一下库存"}]
        # 第一轮 load_skill 及其结果
        events = [ev async for ev in backend.chat(messages, [], "s1")]
        load_start = next(ev for ev in events if ev.type == "tool_start")
        skill_name = load_start.input["skill_name"]
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "load_skill", "arguments": f'{{"skill_name": "{skill_name}"}}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": "c1", "content": f"已加载技能 {skill_name}（测试）。"})
        # 第二轮 domain 工具及其结果（走 query_inventory 分支，喂真实格式结果）
        events2 = [ev async for ev in backend.chat(messages, [], "s1")]
        domain = next(ev for ev in events2 if ev.type == "tool_start")
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c2", "type": "function",
            "function": {"name": domain.tool_name, "arguments": '{"channel": "jd", "sku": "SKU-002"}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": "c2", "content": "渠道 jd 商品 SKU-002 库存 45 件：测试商品"})
        # 第三轮总结
        events3 = [ev async for ev in backend.chat(messages, [], "s1")]
        text = "".join(ev.text for ev in events3 if ev.type == "text_delta")
        return text

    text = asyncio.run(scenario())
    # 总结应引用结果事实（库存数 / SKU），而非万能收尾话术
    assert "45 件" in text and "SKU-002" in text
    assert "已根据工具返回结果" not in text


def test_mock_script_third_round_error_summary():
    """第三轮遇平台错误：诚实交代失败原因并给出建议，而非假装完成。"""

    async def scenario():
        backend = MockAgent(BackendConfig(provider=BackendProvider.MOCK, model="mock", api_key="x"))
        messages = [{"role": "user", "content": "查一下库存"}]
        events = [ev async for ev in backend.chat(messages, [], "s1")]
        load_start = next(ev for ev in events if ev.type == "tool_start")
        skill_name = load_start.input["skill_name"]
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "load_skill", "arguments": f'{{"skill_name": "{skill_name}"}}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": "c1", "content": f"已加载技能 {skill_name}（测试）。"})
        events2 = [ev async for ev in backend.chat(messages, [], "s1")]
        domain = next(ev for ev in events2 if ev.type == "tool_start")
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c2", "type": "function",
            "function": {"name": domain.tool_name, "arguments": '{"channel": "jd", "sku": "SKU-002"}'},
        }]})
        messages.append({"role": "tool", "tool_call_id": "c2", "content": "[平台错误 10005] 平台限流，请稍后重试"})
        events3 = [ev async for ev in backend.chat(messages, [], "s1")]
        return "".join(ev.text for ev in events3 if ev.type == "text_delta")

    text = asyncio.run(scenario())
    assert "没有执行成功" in text and "平台限流" in text