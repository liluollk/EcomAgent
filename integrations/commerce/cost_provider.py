"""内部成本价查询边界。

成本价属于规则判断所需的内部数据，不属于普通库存领域结果。
"""

from __future__ import annotations

from typing import Protocol


class CostProvider(Protocol):
    """为调价安全规则提供可信成本价的窄接口。"""

    def get_cost_price(self, channel: str | None, sku: str) -> float | None:
        """返回成本价；没有可信数据时返回 None。"""


class MockCostProvider:
    """当前 Mock Commerce 数据对应的内部成本实现。"""

    def get_cost_price(self, channel: str | None, sku: str) -> float | None:
        from mock_commerce.store import get_cost_price

        return get_cost_price(channel or "", sku)


DEFAULT_COST_PROVIDER: CostProvider = MockCostProvider()


def get_cost_provider() -> CostProvider:
    """返回当前调价安全规则使用的成本提供方。"""
    return DEFAULT_COST_PROVIDER
