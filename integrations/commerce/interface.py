"""Commerce 接入契约 — 本版本的核心是「多平台商品调价控制面」。

两个协议，边界分明：

  PriceControlProvider（核心，本版本主线）
      调价闭环的稳定接口，就是 price_models.PricePlatform：快照查询、写入、
      回查。Agent Tool → 领域命令 → 策略/审批 → 操作状态机 → Adapter
      这条链路只依赖它。

  CommerceProvider（扩展面，不进默认 Agent 工具表）
      客服工单、知识库、经营统计等相邻能力。它们曾与 11 个平台操作混在
      一个「平台能力」概念里，容易被读成「Agent 能操作平台的一切」。
      现在明确区分：这些是扩展能力，通过独立注册入口提供，默认 Agent
      看不到，也不参与调价评测链路。

平台差异（参数信封、签名字段、价格精度、错误码）由各平台 Adapter 吸收，
上层编排对平台无感知。
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
from integrations.commerce.price_models import PricePlatform

__all__ = [
    "CommerceProvider",
    "PriceControlProvider",
]


class PriceControlProvider(PricePlatform, Protocol):
    """调价闭环的稳定接口（继承 PricePlatform，不另起一套形状）。

    query_snapshot / apply_price / verify_price 三个动作的语义见 price_models。
    """

    ...


@runtime_checkable
class CommerceProvider(Protocol):
    """扩展面接口 — 平台周边的相邻业务能力。

    这些方法**不是**本版本的调价控制面，也不在默认 Agent 工具表中；
    保留协议是为了扩展插件的类型边界，不代表它们属于真实生产平台接入。
    """

    async def query_inventory(self, sku_id: str, channel: str = "") -> InventoryResult:
        """查询商品库存。"""
        ...

    async def update_price(
        self,
        sku_id: str,
        price: Decimal,
        channel: str = "",
        idempotency_key: str | None = None,
    ) -> UpdatePriceResult:
        """更新商品价格（扩展面入口；主线走 PriceControlProvider）。"""
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
