"""内部成本查询边界测试。"""

from integrations.commerce.cost_provider import MockCostProvider


def test_mock_cost_provider_reads_internal_platform_cost():
    provider = MockCostProvider()

    assert provider.get_cost_price("taobao", "SKU-001") == 59.0
    assert provider.get_cost_price("unknown", "SKU-001") is None
    assert provider.get_cost_price("taobao", "SKU-999") is None
