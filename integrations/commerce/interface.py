"""
CommerceProvider Protocol — 电商平台接入的稳定领域接口。

Runtime 只知道这个 Protocol，不知道任何具体平台（淘宝/京东/抖音）的细节。
具体平台通过 Adapter 实现此接口，Runtime 一行代码不需要动。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from integrations.commerce.models import (
    AfterSalesStatsResult,
    AnomaliesResult,
    InventoryResult,
    KnowledgeResult,
    OrderStatsResult,
    OrderStatusResult,
    PromotionRequest,
    PromotionResult,
    PromotionsResult,
    ServiceTicketResult,
    ShelfAction,
    ShelfResult,
    UpdatePriceResult,
)


@runtime_checkable
class CommerceProvider(Protocol):
    """电商平台接入的稳定领域接口。

    所有方法返回领域结果类型，不暴露 HTTP Response。
    """

    async def query_inventory(self, sku_id: str, channel: str = "") -> InventoryResult:
        """查询商品库存。"""
        ...

    async def update_price(
        self,
        sku_id: str,
        price: Decimal,
        channel: str = "",
        cost_price: Decimal = Decimal("0"),
        idempotency_key: str | None = None,
    ) -> UpdatePriceResult:
        """更新商品价格。"""
        ...

    async def product_shelf(
        self,
        sku_id: str,
        action: ShelfAction,
        channel: str = "",
    ) -> ShelfResult:
        """商品上下架操作。"""
        ...

    async def create_promotion(
        self,
        request: PromotionRequest,
        idempotency_key: str | None = None,
    ) -> PromotionResult:
        """创建促销活动。"""
        ...

    async def query_order_status(
        self,
        order_id: str,
        channel: str = "",
    ) -> OrderStatusResult:
        """查询订单状态。"""
        ...

    async def query_order_stats(
        self,
        channel: str = "",
        period: str = "近7天",
    ) -> OrderStatsResult:
        """查询订单统计。"""
        ...

    async def create_service_ticket(
        self,
        order_id: str,
        issue: str,
        channel: str = "",
        priority: str = "normal",
        idempotency_key: str | None = None,
    ) -> ServiceTicketResult:
        """创建售后工单。"""
        ...

    async def query_anomalies(self, channel: str = "") -> AnomaliesResult:
        """查询经营异常。"""
        ...

    async def query_promotions(self, channel: str = "") -> PromotionsResult:
        """查询进行中的促销。"""
        ...

    async def query_after_sales_stats(
        self,
        channel: str = "",
        period: str = "近7天",
    ) -> AfterSalesStatsResult:
        """查询售后统计。"""
        ...

    async def query_knowledge_base(self, topic: str) -> KnowledgeResult:
        """查询知识库。"""
        ...