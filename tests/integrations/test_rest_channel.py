"""REST 渠道层测试 — RestClient 单测、mock 平台端点、内置工具全链路。"""

import asyncio

import httpx
import pytest

from mock_commerce.routes import app as channel_api_app
from integrations.commerce.client import ChannelRestClient, RestApiError
from integrations.mcp.client_pool import McpClientPool


class MockResponder:
    """httpx MockTransport：按 URL 关键字返回模拟平台响应。"""

    def __init__(self, responses: dict[str, dict]) -> None:
        self._responses = responses

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path
        if key in self._responses:
            payload = self._responses[key]
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": 10002, "message": f"渠道不存在: {key}"})


def _run(coro):
    return asyncio.run(coro)


def test_rest_client_ok_and_mapping():
    """正常返回取 data；业务错误码 → RestApiError（含 code/message）。"""
    mock = MockResponder({
        "/v1/taobao/inventory": {"code": 0, "message": "ok", "data": {"sku": "SKU-1", "stock": 5}},
        "/v1/taobao/price": {"code": 10004, "message": "业务状态不允许（如促销已存在）"},
    })
    client = ChannelRestClient(
        base_url="http://platform.test",
        transport=httpx.MockTransport(mock.handler),
    )

    async def scenario():
        data = await client.call("GET", "/v1/taobao/inventory", params={"sku": "SKU-1"})
        assert data == {"sku": "SKU-1", "stock": 5}
        with pytest.raises(RestApiError) as ei:
            await client.call("PUT", "/v1/taobao/price")
        assert ei.value.code == 10004
        assert "业务状态" in ei.value.message
        await client.aclose()

    _run(scenario())


def test_rest_client_platform_error_text_map():
    """未知业务码回落到 PLATFORM_ERROR_TEXT 语义。"""
    mock = MockResponder({"/v1/x": {"code": 99999, "message": ""}})
    client = ChannelRestClient(base_url="http://t", transport=httpx.MockTransport(mock.handler))

    async def scenario():
        with pytest.raises(RestApiError) as ei:
            await client.call("GET", "/v1/x")
        assert "未知平台错误码" in ei.value.message
        await client.aclose()

    _run(scenario())


@pytest.mark.asyncio
async def test_channel_api_idempotency():
    """平台侧幂等：同幂等键重复 POST 不重复创建，返回回放标记。"""
    transport = httpx.ASGITransport(app=channel_api_app)
    client = ChannelRestClient(base_url="http://test", transport=transport)

    key = "idem-demo-001"
    body = {"sku": "SKU-9", "discount": 0.8, "start_time": "t1", "end_time": "t2"}
    first = await client.call("POST", "/v1/taobao/promotions", json_body=body, idempotency_key=key)
    assert first.get("idempotent_replay") is None or first.get("idempotent_replay") is False
    second = await client.call("POST", "/v1/taobao/promotions", json_body=body, idempotency_key=key)
    assert second.get("idempotent_replay") is True
    assert second["sku"] == first["sku"] and second["discount"] == first["discount"]
    # 同 key 不同 body 也仅保留首次结果（平台语义：key 唯一）
    third = await client.call("POST", "/v1/taobao/promotions",
                              json_body={"sku": "OTHER", "discount": 0.5, "start_time": "t", "end_time": "t"},
                              idempotency_key=key)
    assert third["sku"] == "SKU-9"
    # 无 key 正常创建
    fresh = await client.call("POST", "/v1/taobao/promotions", json_body=body)
    assert fresh.get("idempotent_replay") is not True
    await client.aclose()


class RetryCapture:
    """MockTransport：记录请求，前两次返回限流错误码，第三次成功。"""

    def __init__(self) -> None:
        self.headers: list[dict] = []
        self.count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        self.headers.append(dict(request.headers))
        if self.count < 3:
            return httpx.Response(200, json={"code": 10005, "message": "平台限流，请稍后重试"})
        return httpx.Response(200, json={"code": 0, "message": "ok",
                                          "data": {"sku": "SKU-1", "discount": 0.8}})


@pytest.mark.asyncio
async def test_rest_client_single_attempt_no_retry():
    """客户端单次请求语义：限流直接抛错、不重试——重试职责上收 Execution Policy
    （「重试复用同一幂等键」的契约在 tests/execution/test_execution_policy.py 验证）。"""
    capture = RetryCapture()
    client = ChannelRestClient(
        base_url="http://t",
        transport=httpx.MockTransport(capture.handler),
    )
    with pytest.raises(RestApiError) as ei:
        await client.call("POST", "/v1/taobao/promotions", json_body={"sku": "SKU-1"})
    assert ei.value.code == 10005
    assert capture.count == 1, "客户端应保持单次请求语义，重试由策略层负责"
    await client.aclose()


async def test_channel_api_all_endpoints():
    """mock 平台端点：鉴权、渠道校验、正常数据（经 ASGI transport 真实 HTTP 语义）。"""
    transport = httpx.ASGITransport(app=channel_api_app)
    client = ChannelRestClient(base_url="http://test", transport=transport)

    # 正常库存
    data = await client.call("GET", "/v1/taobao/inventory", params={"sku": "SKU-001"})
    assert data["stock"] == 1523 and data["name"].startswith("海洋")
    # 渠道不存在 → 平台业务码 10002
    with pytest.raises(RestApiError) as ei:
        await client.call("GET", "/v1/meta/inventory", params={"sku": "SKU-001"})
    assert ei.value.code == 10002
    # 鉴权失败（缺 key）→ 401 + 10003
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=channel_api_app)) as raw:
        resp = await raw.get("http://test/v1/taobao/inventory", params={"sku": "S"})
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == 10003
    # 售后分析结构化数据
    data = await client.call("GET", "/v1/douyin/after-sales-stats", params={"period": "近7天"})
    assert data["refund_rate"] == 0.038
    await client.aclose()


def test_builtin_tools_over_rest_full_chain():
    """全链路：内置平台 API 工具 → 渠道注册表 → RestClient → mock 平台服务。

    调价闭环（默认工具）在内置通道（sources/builtin_tools.py）执行，走平台默认路径：
    渠道注册表解析 client（进程内服务），验证完整 REST 语义（鉴权 / 信封 / 错误码）。
    扩展工具（客服 / 知识库 / 经营分析等）走同一通道，但不在默认 Agent 工具表里。
    """
    from sources import builtin_tools

    async def scenario():
        # 默认 Agent 只看到调价闭环 + 元技能
        assert set(builtin_tools.tool_names()) == {
            "query_product_snapshot", "update_price", "save_skill",
        }
        # 调价闭环：快照 → 提交目标价（含平台回查）
        snap = await builtin_tools.query_product_snapshot("taobao", "ITEM-1001", "SKU-002")
        assert "当前价" in snap and "库存" in snap
        updated = await builtin_tools.update_price(
            "taobao", "ITEM-1001", "SKU-002", 99.0, operation_id="op-rest-chain",
        )
        assert "已更新" in updated and "回查一致" in updated

        # 扩展工具仍在同一通道可用，但不在默认工具表
        ext = builtin_tools.get_extension_handlers()
        assert "query_knowledge_base" in ext and "query_knowledge_base" not in builtin_tools.get_handlers()
        kb = await ext["query_knowledge_base"]("退款政策")
        assert "知识库" in kb and "退款" in kb

        # 平台错误码链路：未知渠道 → 10002 错误文本透传
        anomalies = await ext["query_anomalies"]("no-such")
        assert "[平台错误 10002]" in anomalies

    asyncio.run(scenario())
