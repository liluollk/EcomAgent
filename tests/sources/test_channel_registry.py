"""ChannelRegistry 测试 — 渠道配置化接入骨架。

验证：默认三渠道、新增/修改/删除、敏感字段掩码、per-channel REST client
离线直连 mock 平台、动态新增渠道即时可用（11 工具按 channel 分派）。
"""

import pytest

import sources.channel_registry as _cr
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY, MOCK_API_KEY


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """隔离的注册表：独立配置文件 + 清空模块缓存，从默认三渠道开始。"""
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    _cr._store._mtime = -1
    _cr._store._channels = None
    DEFAULT_CHANNEL_REGISTRY._clients.clear()
    yield DEFAULT_CHANNEL_REGISTRY
    DEFAULT_CHANNEL_REGISTRY._clients.clear()


def test_default_three_channels(registry):
    names = {c["name"] for c in registry.list()}
    assert names == {"taobao", "jd", "douyin"}
    assert registry.enabled_names() == {"taobao", "jd", "douyin"}


def test_add_channel_and_mask(registry):
    added = registry.add({
        "name": "pdd",
        "label": "拼多多",
        "auth_type": "api_key",
        "api_key": "sk-live-123456",
    })
    # 掩码：不落明文
    assert added["api_key"] == "sk-l****"
    listed = {c["name"]: c for c in registry.list()}
    assert listed["pdd"]["label"] == "拼多多"
    assert listed["pdd"]["auth_type"] == "api_key"
    assert listed["pdd"]["api_key"] == "sk-l****"
    assert "pdd" in registry.enabled_names()


def test_add_duplicate_raises(registry):
    with pytest.raises(ValueError):
        registry.add({"name": "taobao"})


def test_update_channel(registry):
    registry.add({"name": "pdd", "label": "拼多多"})
    registry.update("pdd", {"enabled": False})
    assert registry.get("pdd")["enabled"] is False
    assert "pdd" not in registry.enabled_names()  # 停用后不再放行


def test_remove_builtin_blocked_and_custom_ok(registry):
    with pytest.raises(ValueError):
        registry.remove("taobao")
    registry.add({"name": "pdd"})
    registry.remove("pdd")
    assert registry.get("pdd") is None


def test_client_for_default_channel_offline(registry):
    """默认渠道 client 离线直连 mock 平台，返回真实业务数据。"""
    client = registry.client_for("taobao")
    assert client is not None
    data = _await_call(client, "GET", "/v1/taobao/order-stats")
    assert data.get("orders") and data["orders"] > 0


def test_client_for_disabled_returns_none(registry):
    registry.add({"name": "pdd", "label": "拼多多", "enabled": False})
    assert registry.client_for("pdd") is None


def test_dynamic_added_channel_usable(registry):
    """新增渠道即时可用：client 离线直连 mock 平台，放行 + 通用数据 fallback。"""
    registry.add({"name": "pdd", "label": "拼多多"})
    client = registry.client_for("pdd")
    assert client is not None
    data = _await_call(client, "GET", "/v1/pdd/order-stats")
    assert "orders" in data  # 通用 fallback（0），不报"渠道不存在"


def test_real_base_url_uses_tcp_not_asgi(registry):
    """显式配置真实 base_url 时走真实 TCP（不注入 ASGI 离线 transport）。"""
    registry.add({"name": "real", "label": "真实平台", "base_url": "http://127.0.0.1:9"})
    client = registry.client_for("real")
    assert client is not None
    # 连不上的地址应抛连接类错误（说明走真实 TCP 而非离线 ASGI）
    with pytest.raises(Exception):
        _await_call(client, "GET", "/v1/real/order-stats")


def test_default_channels_have_platform_mock(registry):
    for c in registry.list():
        assert c["platform"] == "mock"


def test_platform_field_normalize_and_invalid_fallback(registry):
    added = registry.add({"name": "pdd", "label": "拼多多", "platform": "taobao"})
    assert added["platform"] == "taobao"
    # 非法 platform 回退 mock
    bad = registry.add({"name": "badp", "label": "非法", "platform": "kuaishou"})
    assert bad["platform"] == "mock"


def test_platform_field_legacy_backfill(registry):
    """旧配置缺 platform 字段时读路径回填 mock（load 不做 normalize）。"""
    import json
    import os

    # 模拟旧版 channels.json：写入不含 platform 字段的配置
    path = os.environ["CHANNEL_CONFIG_FILE"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "channels": [
                {"name": "pdd", "label": "拼多多", "base_url": "", "auth_type": "mock", "enabled": True}
            ]
        }, f, ensure_ascii=False)
    registry._clients.clear()
    listed = {c["name"]: c for c in registry.list()}
    assert listed["pdd"]["platform"] == "mock"
    assert listed["pdd"]["options"] == {}


def test_options_field_and_masking(registry):
    added = registry.add({
        "name": "pdd",
        "label": "拼多多",
        "platform": "taobao",
        "options": {"app_key": "app-123", "app_secret": "sec-abc-9999", "access_token": "tok-xyz"},
    })
    # 返回已掩码
    assert added["options"]["app_secret"].endswith("****")
    assert added["options"]["access_token"].endswith("****")
    # 内存/落盘是明文（get 未掩码）
    got = registry.get("pdd")
    assert got["options"]["app_secret"] == "sec-abc-9999"


def test_non_mock_client_has_no_mock_key_header(registry):
    """真实平台渠道的 client 不携带 mock 默认鉴权头。"""
    registry.add({"name": "real", "label": "真实", "platform": "taobao", "base_url": "http://127.0.0.1:9"})
    client = registry.client_for("real")
    assert client is not None
    assert "X-Api-Key" not in client._headers


def test_cache_key_includes_platform(registry):
    """切换 platform（base_url/鉴权不变）时 client 缓存必须失效重建。"""
    registry.add({"name": "dup", "label": "渠道", "platform": "mock", "base_url": "http://127.0.0.1:9"})
    c1 = registry.client_for("dup")
    registry.update("dup", {"platform": "taobao"})
    c2 = registry.client_for("dup")
    assert c1 is not c2


def test_executor_for_dispatches_by_platform(registry):
    from integrations.commerce.adapter import MockAdapter, TaobaoAdapter, JdAdapter

    assert isinstance(registry.executor_for("taobao"), MockAdapter)  # 内置默认 mock
    assert isinstance(registry.executor_for(None), MockAdapter)
    registry.add({"name": "real", "label": "真实", "platform": "taobao"})
    assert isinstance(registry.executor_for("real"), TaobaoAdapter)
    registry.update("real", {"platform": "jd"})
    assert isinstance(registry.executor_for("real"), JdAdapter)


def _await_call(client, method, path):
    import asyncio

    return asyncio.run(client.call(method, path))
