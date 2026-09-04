"""平台适配器测试 — 抽象接缝、MockAdapter 操作映射、stub 适配器与注册表。"""

import pytest

from sources.platform_adapter import (
    ADAPTER_REGISTRY,
    PLATFORM_KINDS,
    MockAdapter,
    TaobaoAdapter,
    JdAdapter,
    DouyinAdapter,
    GenericOpenAdapter,
    get_adapter,
    platform_is_real,
)


def test_platform_kinds():
    assert PLATFORM_KINDS == ["mock", "taobao", "jd", "douyin", "open"]


def test_get_adapter_known_and_unknown():
    assert isinstance(get_adapter("mock"), MockAdapter)
    assert isinstance(get_adapter("taobao"), TaobaoAdapter)
    assert isinstance(get_adapter("jd"), JdAdapter)
    assert isinstance(get_adapter("douyin"), DouyinAdapter)
    assert isinstance(get_adapter("open"), GenericOpenAdapter)
    # 未知平台回退 mock，保证默认行为不退化
    assert isinstance(get_adapter("nope"), MockAdapter)
    assert isinstance(get_adapter(""), MockAdapter)


def test_platform_is_real():
    assert not platform_is_real("mock")
    assert not platform_is_real("")
    assert not platform_is_real(None)
    assert platform_is_real("taobao")
    assert platform_is_real("jd")
    assert platform_is_real("douyin")
    assert platform_is_real("open")
    # 未知字符串按真实处理会导致 base_url 缺失时仍走真实 TCP —— 但 normalize 已约束取值
    assert platform_is_real("nope") is False


def test_mock_adapter_build_request_all_operations():
    m = MockAdapter()
    cases = {
        "query_inventory": ("GET", "/v1/taobao/inventory", "params"),
        "update_price": ("PUT", "/v1/taobao/price", "json_body"),
        "create_promotion": ("POST", "/v1/taobao/promotions", "json_body"),
        "query_order_status": ("GET", "/v1/taobao/orders/O1", "params"),
        "product_shelf": ("PUT", "/v1/taobao/shelf", "json_body"),
        "service_ticket": ("POST", "/v1/taobao/service-tickets", "json_body"),
        "query_order_stats": ("GET", "/v1/taobao/order-stats", "params"),
        "query_anomalies": ("GET", "/v1/taobao/anomalies", "params"),
        "query_promotions": ("GET", "/v1/taobao/promotions", "params"),
        "query_after_sales_stats": ("GET", "/v1/taobao/after-sales-stats", "params"),
    }
    for op, (method, path, kind) in cases.items():
        req = m.build_request(op, "taobao", {"order_id": "O1"})
        assert req[0] == method
        assert req[1] == path
        assert list(req[2].keys()) == [kind]


def test_mock_adapter_knowledge_base_platform_level():
    m = MockAdapter()
    method, path, kwargs = m.build_request("query_knowledge_base", None, {"topic": "退货"})
    assert method == "GET"
    assert path == "/v1/knowledge-base"
    assert kwargs["params"] == {"topic": "退货"}


def test_mock_adapter_unknown_operation_raises():
    m = MockAdapter()
    with pytest.raises(ValueError):
        m.build_request("no_such_op", "taobao", {})


def test_mock_adapter_requires_channel_for_channel_ops():
    m = MockAdapter()
    with pytest.raises(ValueError):
        m.build_request("query_inventory", None, {})


def test_parse_response_identity():
    """mock 的 call() 已解包到 data dict，parse_response 恒等。"""
    m = MockAdapter()
    payload = {"stock": 42, "name": "连衣裙"}
    assert m.parse_response(payload) == payload


def test_mock_probe_ok():
    import asyncio

    result = asyncio.run(MockAdapter().probe({"platform": "mock"}))
    assert result["ok"] is True


def test_stub_adapters_honest_probe():
    """真实平台 stub 的 probe 诚实返回「尚未接入」，不误报连通。"""
    import asyncio

    for kind in ("taobao", "jd", "douyin", "open"):
        adapter = get_adapter(kind)
        result = asyncio.run(adapter.probe({"platform": kind}))
        assert result["ok"] is False
        assert "尚未接入" in result["message"]


def test_stub_build_request_raises():
    with pytest.raises(NotImplementedError):
        TaobaoAdapter().build_request("query_inventory", "taobao", {})


def test_adapter_registry_keys():
    assert set(ADAPTER_REGISTRY.keys()) == set(PLATFORM_KINDS)