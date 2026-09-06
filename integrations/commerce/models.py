"""
Commerce 领域结果类型 — Adapter 将外部 API 响应转换为 Runtime 可理解的领域结果。

不暴露 HTTP Response，只返回领域类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class ShelfAction(Enum):
    ON = "on"
    OFF = "off"


@dataclass
class InventoryResult:
    sku_id: str
    name: str
    stock: int
    channel: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class UpdatePriceResult:
    sku_id: str
    price: Decimal
    channel: str = ""
    idempotent_replay: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShelfResult:
    sku_id: str
    action: ShelfAction
    channel: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromotionRequest:
    sku_id: str
    discount: float
    start_time: str
    end_time: str
    channel: str = ""


@dataclass
class PromotionResult:
    sku_id: str
    discount: float
    start_time: str
    end_time: str
    channel: str = ""
    idempotent_replay: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderStatusResult:
    order_id: str
    status: str
    channel: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderStatsResult:
    orders: int
    gmv: float
    avg: float
    channel: str = ""
    period: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceTicketResult:
    ticket_id: str
    order_id: str
    channel: str = ""
    idempotent_replay: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnomaliesResult:
    channel: str
    items: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromotionsResult:
    channel: str
    items: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AfterSalesStatsResult:
    channel: str
    refund_rate: float
    tickets: int
    period: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeResult:
    topic: str
    matched: bool
    text: str | None = None
    key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)