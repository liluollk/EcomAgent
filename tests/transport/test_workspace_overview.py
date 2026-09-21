"""工作台聚合路由测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

import sources.channel_registry as _cr
from transport.server import app
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
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
    async with await _client() as client:
        resp = await client.get("/workspace/overview")
        assert resp.status_code == 200
        data = resp.json()
        names = {c["name"] for c in data["channels"]}
        assert names == {"taobao", "douyin"}

        for ch in data["channels"]:
            assert ch["connected"] is True
            assert ch["error"] is None
            assert isinstance(ch["orders"], int) and ch["orders"] > 0
            assert isinstance(ch["gmv"], (int, float)) and ch["gmv"] > 0
            assert isinstance(ch["promotions"], list) and len(ch["promotions"]) >= 1
            assert isinstance(ch["anomalies"], list)
            assert ch["product"] and ch["product"]["name"] and ch["product"]["stock"] > 0

        s = data["summary"]
        assert s["connected_channels"] == 2
        assert s["total_orders"] == sum(c["orders"] for c in data["channels"])
        assert s["total_promotions"] == sum(len(c["promotions"]) for c in data["channels"])


async def test_overview_disabled_channel_excluded():
    async with await _client() as client:
        resp = await client.patch("/sources/douyin", json={"enabled": False})
        assert resp.status_code == 200
        data = (await client.get("/workspace/overview")).json()
        names = {c["name"] for c in data["channels"]}
        assert "douyin" not in names


async def test_overview_real_platform_channel_marked_not_connected():
    async with await _client() as client:
        resp = await client.post("/sources", json={
            "name": "tbprod",
            "label": "淘宝生产店",
            "platform": "taobao",
            "base_url": "https://example.invalid/openapi",
            "options": {"app_key": "k", "app_secret": "s"},
        })
        assert resp.status_code == 200
        data = (await client.get("/workspace/overview")).json()
        tb = next(c for c in data["channels"] if c["name"] == "tbprod")
        assert tb["connected"] is False
        assert "适配器未接入" in tb["error"]


async def test_overview_added_mock_channel_included():
    async with await _client() as client:
        resp = await client.post("/sources", json={"name": "shop2", "label": "抖音店"})
        assert resp.status_code == 200
        data = (await client.get("/workspace/overview")).json()
        shop = next(c for c in data["channels"] if c["name"] == "shop2")
        assert shop["connected"] is True
        assert shop["label"] == "抖音店"
