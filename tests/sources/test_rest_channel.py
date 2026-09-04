"""REST 渠道层测试 — RestClient 单测、mock 平台端点、MCP→REST 全链路。"""

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from mocks.channel_api_mock import app as channel_api_app
from sources.rest_client import ChannelRestClient, RestApiError
from sources.mcp_client_pool import McpClientPool


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
async def test_rest_client_retry_reuses_idempotency_key():
    """限流重试：3 次请求带同一 X-Idempotency-Key（防止重试重复创建）。"""
    capture = RetryCapture()
    client = ChannelRestClient(
        base_url="http://t",
        retries=2,
        retry_delay=0.01,
        transport=httpx.MockTransport(capture.handler),
    )
    data = await client.call("POST", "/v1/taobao/promotions", json_body={"sku": "SKU-1"})
    assert data["sku"] == "SKU-1"
    assert capture.count == 3
    keys = {h.get("x-idempotency-key") for h in capture.headers}
    assert len(keys) == 1 and list(keys)[0]
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout: float = 6.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=0.5):
                return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError(f"channel api mock not ready on {port}")


def test_mcp_over_rest_full_chain():
    """全链路：MCP Server（子进程）→ RestClient → mock 平台服务（子进程），真实协议 + 真实 HTTP。"""
    port = _free_port()
    platform_proc = subprocess.Popen(
        [sys.executable, "-m", "mocks.channel_api_mock", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    old_url = os.environ.get("CHANNEL_API_URL")
    os.environ["CHANNEL_API_URL"] = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(port)
        pool = McpClientPool()

        async def scenario():
            await pool.connect()
            try:
                defs = pool.get_all_tool_definitions()
                names = {d["name"] for d in defs}
                assert len(names) == 11
                # 真实调用：MCP → HTTP → 平台数据
                r = await pool.call_tool("query_inventory", {"channel": "taobao", "sku": "SKU-001"})
                assert "库存" in r and "海洋" in r
                r2 = await pool.call_tool("update_price", {"channel": "jd", "sku": "SKU-1", "new_price": 89.0})
                assert "已更新为" in r2
                r3 = await pool.call_tool("query_knowledge_base", {"topic": "退款政策"})
                assert "知识库" in r3 and "退款" in r3
                # 平台错误码链路：未知渠道 → 10002 错误文本透传
                r4 = await pool.call_tool("query_anomalies", {"channel": "no-such"})
                assert "[平台错误 10002]" in r4
            finally:
                await pool.close()

        asyncio.run(scenario())
    finally:
        platform_proc.terminate()
        try:
            platform_proc.wait(timeout=5)
        except Exception:
            platform_proc.kill()
        if old_url is None:
            os.environ.pop("CHANNEL_API_URL", None)
        else:
            os.environ["CHANNEL_API_URL"] = old_url