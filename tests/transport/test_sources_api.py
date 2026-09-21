"""渠道管理 API 测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

import sources.channel_registry as _cr
from transport.server import app
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    monkeypatch.setenv("CHANNEL_API_AUTO", "0")
    _cr._store._mtime = -1
    _cr._store._channels = None
    DEFAULT_CHANNEL_REGISTRY._clients.clear()
    yield
    DEFAULT_CHANNEL_REGISTRY._clients.clear()


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_sources_default():
    async with await _client() as client:
        resp = await client.get("/sources")
        data = resp.json()
        names = {c["name"] for c in data}
        assert names == {"taobao", "douyin"}


async def test_add_and_list_source():
    async with await _client() as client:
        resp = await client.post("/sources", json={"name": "shop2", "label": "抖音店"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "shop2"
        listed = (await client.get("/sources")).json()
        assert any(c["name"] == "shop2" for c in listed)


async def test_patch_channel_credentials_masked():
    async with await _client() as client:
        await client.post("/sources", json={
            "name": "tbshop",
            "label": "淘宝店",
            "platform": "taobao",
            "base_url": "https://example.invalid",
            "auth_type": "api_key",
            "api_key": "sk-secret",
            "options": {"app_key": "ak", "app_secret": "sec", "access_token": "tok"},
        })
        listed = await client.get("/sources")
        item = next(c for c in listed.json() if c["name"] == "tbshop")
        assert item["platform"] == "taobao"
        assert item["options"]["app_secret"].endswith("****")
        assert item["options"]["access_token"].endswith("****")


async def test_patch_channel_platform_switch():
    async with await _client() as client:
        await client.post("/sources", json={"name": "sw", "label": "切换"})
        resp = await client.patch("/sources/sw", json={
            "platform": "douyin",
            "base_url": "https://example.invalid/open",
            "options": {"app_key": "k", "app_secret": "s"},
        })
        assert resp.status_code == 200
        item = (await client.get("/sources")).json()
        sw = next(c for c in item if c["name"] == "sw")
        assert sw["platform"] == "douyin"


async def test_source_connectivity_open_stub():
    """自定义开放平台 open 仍是 stub：测连通诚实返回「尚未接入」。"""
    async with await _client() as client:
        await client.post("/sources", json={"name": "custom", "label": "自定义", "platform": "open"})
        resp = await client.post("/sources/custom/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "尚未接入" in body["message"]


async def test_source_connectivity_contract_only_platform_is_honest():
    """淘宝调价 Adapter 是离线契约实现：测连通可以说 ok，但必须如实声明没有生产接入。"""
    async with await _client() as client:
        await client.post("/sources", json={"name": "tbshop", "label": "淘宝店", "platform": "taobao"})
        resp = await client.post("/sources/tbshop/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "离线契约实现" in body["message"]
        assert "未经真实平台资质" in body["message"]
