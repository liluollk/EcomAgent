"""HttpCommerceProvider 测试 — 领域结果映射（经 ASGI 真实信封语义）。"""

import asyncio

import pytest

from integrations.commerce.client import RestApiError
from integrations.commerce.models import InventoryResult, ShelfAction
from integrations.commerce.provider import get_commerce_provider, invoke_operation


def _run(coro):
    return asyncio.run(coro)


def test_query_inventory_domain_result():
    r = _run(get_commerce_provider("taobao").query_inventory("SKU-001"))
    assert isinstance(r, InventoryResult)
    assert r.stock == 1523
    assert r.name.startswith("海洋")
    assert r.channel == "taobao"


def test_unknown_sku_graceful_empty_row():
    """未知 SKU → mock 空库存行 → 领域结果优雅降级（stock 0 / 空名）。"""
    r = _run(get_commerce_provider("taobao").query_inventory("SKU-999"))
    assert r.stock == 0
    assert r.name == ""


def test_update_price_domain_result():
    r = _run(get_commerce_provider("taobao").update_price("SKU-001", 89.0))
    assert str(r.price) == "89.0"
    assert r.idempotent_replay is False
    assert r.channel == "taobao"


def test_shelf_action_enum_roundtrip():
    r = _run(get_commerce_provider("taobao").product_shelf("SKU-001", ShelfAction.OFF))
    assert r.action is ShelfAction.OFF
    r2 = _run(get_commerce_provider("taobao").product_shelf("SKU-001", "on"))
    assert r2.action is ShelfAction.ON


def test_provider_does_not_expose_http_response():
    """领域结果不携带 HTTP Response：extra 只含归一化业务字段。"""
    r = _run(get_commerce_provider("jd").query_order_stats(period="近7天"))
    assert r.orders == 642 and r.gmv == 51360.0
    assert "status_code" not in r.extra


def test_unknown_channel_raises_rest_api_error():
    with pytest.raises(RestApiError) as ei:
        _run(invoke_operation("no-such-channel", "query_inventory", {"sku": "SKU-001"}))
    assert ei.value.code == 10002
