"""平台适配器测试 — 抽象接缝、MockAdapter 操作映射、stub 适配器与注册表。"""

from integrations.commerce.adapter import (
    ADAPTER_REGISTRY,
    PLATFORM_KINDS,
    MockAdapter,
    TaobaoAdapter,
    DouyinAdapter,
    GenericOpenAdapter,
    get_adapter,
    platform_is_real,
)


def test_platform_kinds():
    assert PLATFORM_KINDS == ["mock", "taobao", "douyin", "open"]


def test_get_adapter_known_and_unknown():
    assert isinstance(get_adapter("mock"), MockAdapter)
    assert isinstance(get_adapter("taobao"), TaobaoAdapter)
    assert isinstance(get_adapter("douyin"), DouyinAdapter)
    assert isinstance(get_adapter("open"), GenericOpenAdapter)
    assert isinstance(get_adapter("nope"), MockAdapter)
    assert isinstance(get_adapter(""), MockAdapter)


def test_platform_is_real():
    assert not platform_is_real("mock")
    assert not platform_is_real("")
    assert not platform_is_real(None)
    assert platform_is_real("taobao")
    assert platform_is_real("douyin")
    assert platform_is_real("open")


def test_registry_contains_core_platforms():
    assert set(ADAPTER_REGISTRY) >= {"mock", "taobao", "douyin", "open"}


def test_stub_open_adapter_honest():
    """open 仍为 stub（诚实返回「尚未接入」）；taobao/douyin 已实现调价执行面。"""
    import asyncio

    from integrations.commerce.adapter import GenericOpenAdapter
    from integrations.commerce.price_models import ProductRef, get_price_platform

    ref = ProductRef(platform="taobao", shop_id="SHOP-01", product_id="ITEM-1001", sku_id="SKU-002")
    for platform in ("taobao", "douyin"):
        adapter = get_price_platform(platform)
        assert callable(adapter.query_snapshot)
    result = asyncio.run(GenericOpenAdapter().probe({"name": "custom", "platform": "open"}))
    assert result["ok"] is False
    assert "尚未接入" in result["message"]


def test_stub_open_build_request_raises():
    GenericOpenAdapter().build_request.__get__(GenericOpenAdapter())
    adapter = GenericOpenAdapter()
    try:
        adapter.build_request("query_inventory", "open", {})
        raise AssertionError("stub 应抛 NotImplementedError")
    except NotImplementedError:
        pass
