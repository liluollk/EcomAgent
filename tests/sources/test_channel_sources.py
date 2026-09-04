"""测试电商渠道 Source — 淘宝、京东、抖音。"""

import pytest
from mocks.channel_sources import (
    create_taobao_source,
    create_jd_source,
    create_douyin_source,
    mock_query_inventory,
    mock_update_price,
    mock_create_promotion,
    mock_query_order_status,
)


def test_create_taobao_source():
    source = create_taobao_source()
    assert source.name == "taobao"
    assert len(source.tools) == 11
    tool_names = [t.name for t in source.tools]
    for name in (
        "query_inventory", "update_price", "create_promotion", "query_order_status",
        "product_shelf", "service_ticket", "query_order_stats", "query_anomalies",
        "query_promotions", "query_after_sales_stats", "query_knowledge_base",
    ):
        assert name in tool_names


def test_create_jd_source():
    source = create_jd_source()
    assert source.name == "jd"
    assert len(source.tools) == 11


def test_create_douyin_source():
    source = create_douyin_source()
    assert source.name == "douyin"
    assert len(source.tools) == 11


def test_mock_query_inventory():
    result = mock_query_inventory("taobao", "SKU-001")
    assert "淘宝" in result
    assert "SKU-001" in str(result)

    result = mock_query_inventory("jd", "SKU-001")
    assert "京东" in result
    assert "北京仓" in str(result)

    result = mock_query_inventory("douyin", "SKU-001")
    assert "抖音" in result


def test_mock_update_price():
    result = mock_update_price("taobao", "SKU-001", 99.9)
    assert "99.9" in result


def test_mock_create_promotion():
    result = mock_create_promotion("taobao", "SKU-001", 0.2, "2026-09-01", "2026-09-15")
    assert "20.0%" in result


def test_mock_query_order_status():
    result = mock_query_order_status("taobao", "ORD-001")
    assert "已发货" in result