"""渠道管理 API 测试 — 设置里新增/修改/删除/测连通渠道（配置化接入骨架）。"""

import pytest
from httpx import ASGITransport, AsyncClient

import sources.channel_registry as _cr
from transport.server import app
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """隔离渠道配置文件，避免污染 data/channels.json 与跨用例缓存。"""
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    _cr._store._mtime = -1
    _cr._store._channels = None
    DEFAULT_CHANNEL_REGISTRY._clients.clear()
    yield
    DEFAULT_CHANNEL_REGISTRY._clients.clear()


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_sources_default_three():
    async with await _client() as client:
        resp = await client.get("/sources")
        data = resp.json()
        names = {c["name"] for c in data}
        assert names == {"taobao", "jd", "douyin"}


async def test_add_and_list_source():
    async with await _client() as client:
        resp = await client.post("/sources", json={"name": "pdd", "label": "拼多多"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "pdd"
        listed = (await client.get("/sources")).json()
        assert any(c["name"] == "pdd" for c in listed)


async def test_add_duplicate_returns_400():
    async with await _client() as client:
        resp = await client.post("/sources", json={"name": "taobao"})
        assert resp.status_code == 400
        assert "已存在" in resp.json()["error"]


async def test_add_missing_name_returns_400():
    async with await _client() as client:
        resp = await client.post("/sources", json={"label": "未命名"})
        assert resp.status_code == 400


async def test_update_source():
    async with await _client() as client:
        await client.post("/sources", json={"name": "pdd", "label": "拼多多"})
        resp = await client.patch("/sources/pdd", json={"enabled": False, "base_url": "http://x:1"})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


async def test_update_missing_returns_404():
    async with await _client() as client:
        resp = await client.patch("/sources/nope", json={"enabled": False})
        assert resp.status_code == 404


async def test_delete_custom_ok_and_builtin_400():
    async with await _client() as client:
        await client.post("/sources", json={"name": "pdd"})
        assert (await client.delete("/sources/pdd")).status_code == 200
        assert (await client.delete("/sources/taobao")).status_code == 400
        assert (await client.delete("/sources/missing")).status_code == 404


async def test_source_connectivity_mock_ok():
    """mock 渠道连通性测试成功（离线直连 mock 平台）。"""
    async with await _client() as client:
        await client.post("/sources", json={"name": "pdd", "label": "拼多多"})
        resp = await client.post("/sources/pdd/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "orders" in body["data"]


async def test_add_source_with_platform_and_options():
    """新增渠道可带 platform 与 options（真实平台配置），敏感字段掩码返回。"""
    async with await _client() as client:
        resp = await client.post("/sources", json={
            "name": "tbshop",
            "label": "淘宝店",
            "platform": "taobao",
            "base_url": "https://eco.taobao.com/router/rest",
            "options": {"app_key": "123456", "app_secret": "secret-zz-8888", "access_token": "tok-abc"},
        })
        assert resp.status_code == 200
        listed = await client.get("/sources")
        item = next(c for c in listed.json() if c["name"] == "tbshop")
        assert item["platform"] == "taobao"
        assert item["options"]["app_secret"].endswith("****")
        assert item["options"]["access_token"].endswith("****")


async def test_patch_channel_platform_switch():
    """编辑渠道：把 platform 从 mock 切成 taobao，冷启动重建 client。"""
    async with await _client() as client:
        await client.post("/sources", json={"name": "sw", "label": "切换"})
        resp = await client.patch("/sources/sw", json={"platform": "jd", "base_url": "https://api.jd.com/routerjson"})
        assert resp.status_code == 200
        item = (await client.get("/sources")).json()
        sw = next(c for c in item if c["name"] == "sw")
        assert sw["platform"] == "jd"


async def test_source_connectivity_real_platform_stub():
    """真实平台（taobao）测连通：stub 诚实返回「尚未接入」，不误报连通。"""
    async with await _client() as client:
        await client.post("/sources", json={"name": "tbshop", "label": "淘宝店", "platform": "taobao"})
        resp = await client.post("/sources/tbshop/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "尚未接入" in body["message"]
