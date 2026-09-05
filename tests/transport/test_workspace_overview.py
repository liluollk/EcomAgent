"""工作台聚合路由测试 — /workspace/overview 真实协议层取数与容错。"""

import pytest
from httpx import ASGITransport, AsyncClient

import sources.channel_registry as _cr
from transport.server import app
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """隔离渠道配置；关闭平台子进程自动拉起（REST client 走离线 ASGI 兜底）。"""
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    monkeypatch.setenv("CHANNEL_API_AUTO", "0")
    monkeypatch.delenv("CHANNEL_API_URL", raising=False)
    _cr._store._mtime = -1
    _cr._store._channels = None
    DEFAULT_CHANNEL_REGISTRY._clients.clear()
    yield
    DEFAULT_CHANNEL_REGISTRY._clients.clear()


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_overview_aggregates_default_channels():
    """内置三渠道全部聚合成功：核心指标 + 促销/异常列表 + 商品。"""
    async with await _client() as client:
        resp = await client.get("/workspace/overview")
        assert resp.status_code == 200
        data = resp.json()
        names = {c["name"] for c in data["channels"]}
        assert names == {"taobao", "jd", "douyin"}

        for ch in data["channels"]:
            assert ch["connected"] is True
            assert ch["error"] is None
            assert isinstance(ch["orders"], int) and ch["orders"] > 0
            assert isinstance(ch["gmv"], (int, float)) and ch["gmv"] > 0
            assert isinstance(ch["promotions"], list) and len(ch["promotions"]) >= 1
            assert isinstance(ch["anomalies"], list)
            assert ch["product"] and ch["product"]["name"] and ch["product"]["stock"] > 0

        s = data["summary"]
        assert s["connected_channels"] == 3
        assert s["total_orders"] == sum(c["orders"] for c in data["channels"])
        assert s["total_promotions"] == sum(len(c["promotions"]) for c in data["channels"])


async def test_overview_disabled_channel_excluded():
    """停用渠道不进入聚合结果。"""
    async with await _client() as client:
        resp = await client.patch("/sources/douyin", json={"enabled": False})
        assert resp.status_code == 200
        data = (await client.get("/workspace/overview")).json()
        names = {c["name"] for c in data["channels"]}
        assert "douyin" not in names


async def test_overview_real_platform_channel_marked_not_connected():
    """真实平台渠道：适配层未接入，返回占位 + 诚实说明，不误报连通。"""
    async with await _client() as client:
        resp = await client.post("/sources", json={"name": "pdd", "label": "拼多多", "platform": "taobao"})
        assert resp.status_code == 200
        data = (await client.get("/workspace/overview")).json()
        pdd = next(c for c in data["channels"] if c["name"] == "pdd")
        assert pdd["connected"] is False
        assert "适配器未接入" in pdd["error"]


async def test_overview_added_mock_channel_included():
    """前端新增的 mock 渠道即时进入聚合（配置化生效）。"""
    async with await _client() as client:
        resp = await client.post("/sources", json={"name": "pdd", "label": "拼多多"})
        assert resp.status_code == 200
        data = (await client.get("/workspace/overview")).json()
        pdd = next(c for c in data["channels"] if c["name"] == "pdd")
        assert pdd["connected"] is True
        assert pdd["label"] == "拼多多"
