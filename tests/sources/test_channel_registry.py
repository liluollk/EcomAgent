"""ChannelRegistry 测试 — 渠道配置化接入骨架。"""

import pytest

import sources.channel_registry as _cr
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY, MOCK_API_KEY


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    _cr._store._mtime = -1
    _cr._store._channels = None
    DEFAULT_CHANNEL_REGISTRY._clients.clear()
    yield DEFAULT_CHANNEL_REGISTRY
    DEFAULT_CHANNEL_REGISTRY._clients.clear()


def test_default_channels(registry):
    names = {c["name"] for c in registry.list()}
    assert names == {"taobao", "douyin"}
    assert registry.enabled_names() == {"taobao", "douyin"}


def test_add_channel_and_mask(registry):
    added = registry.add({
        "name": "shop2",
        "label": "抖音店",
        "auth_type": "api_key",
        "api_key": "sk-live-123456",
    })
    assert added["api_key"] == "sk-l****"
    listed = {c["name"]: c for c in registry.list()}
    assert listed["shop2"]["label"] == "抖音店"
    assert listed["shop2"]["auth_type"] == "api_key"
    assert listed["shop2"]["api_key"] == "sk-l****"
    assert "shop2" in registry.enabled_names()


def test_add_duplicate_raises(registry):
    with pytest.raises(ValueError):
        registry.add({"name": "taobao"})


def test_update_channel(registry):
    registry.add({"name": "shop2", "label": "抖音店"})
    registry.update("shop2", {"enabled": False})
    assert registry.get("shop2")["enabled"] is False
    assert "shop2" not in registry.enabled_names()


def test_remove_builtin_blocked_and_custom_ok(registry):
    with pytest.raises(ValueError):
        registry.remove("taobao")
    registry.add({"name": "shop2"})
    registry.remove("shop2")
    assert registry.get("shop2") is None


def test_client_for_default_channel_offline(registry):
    client = registry.client_for("taobao")
    assert client is not None
    data = _await_call(client, "GET", "/v1/taobao/order-stats")
    assert data.get("orders") and data["orders"] > 0


def test_client_for_disabled_returns_none(registry):
    registry.add({"name": "shop2", "label": "抖音店", "enabled": False})
    assert registry.client_for("shop2") is None


def test_dynamic_added_channel_usable(registry):
    registry.add({"name": "shop2", "label": "自定义店"})
    client = registry.client_for("shop2")
    assert client is not None
    data = _await_call(client, "GET", "/v1/shop2/order-stats")
    assert "orders" in data


def test_real_base_url_uses_tcp_not_asgi(registry):
    registry.add({"name": "real", "label": "真实平台", "base_url": "http://127.0.0.1:9"})
    client = registry.client_for("real")
    assert client is not None
    with pytest.raises(Exception):
        _await_call(client, "GET", "/v1/real/order-stats")


def test_default_channels_effective_platform_mock(registry):
    for c in registry.list():
        assert c["platform"] == "mock"
        assert registry.effective_platform(c["name"]) == "mock"


def test_platform_field_normalize_and_invalid_fallback(registry):
    added = registry.add({"name": "shop2", "label": "抖音店", "platform": "douyin"})
    assert added["platform"] == "douyin"
    bad = registry.add({"name": "badp", "label": "非法", "platform": "notaplat"})
    assert bad["platform"] == "mock"


def test_platform_field_legacy_backfill(registry):
    import json
    import os

    path = os.environ["CHANNEL_CONFIG_FILE"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "channels": [{
                "name": "legacy",
                "label": "旧配置",
                "base_url": "",
                "auth_type": "mock",
                "enabled": True,
            }]
        }, f, ensure_ascii=False)
    _cr._store._mtime = -1
    _cr._store._channels = None
    cfg = registry.get("legacy")
    assert cfg["platform"] == "mock"


def test_executor_for_dispatches_by_platform(registry):
    from integrations.commerce.adapter import MockAdapter, TaobaoAdapter, DouyinAdapter

    assert isinstance(registry.executor_for("taobao"), MockAdapter)
    assert isinstance(registry.executor_for(None), MockAdapter)
    registry.add({
        "name": "real",
        "label": "真实",
        "platform": "taobao",
        "base_url": "https://example.invalid/openapi",
        "options": {"app_key": "k", "app_secret": "s"},
    })
    assert isinstance(registry.executor_for("real"), TaobaoAdapter)
    registry.update("real", {"platform": "douyin"})
    assert isinstance(registry.executor_for("real"), DouyinAdapter)


def _await_call(client, method, path):
    import asyncio

    return asyncio.run(client.call(method, path))
