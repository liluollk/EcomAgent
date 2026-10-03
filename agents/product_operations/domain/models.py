"""商品域领域模型(架构文档 §3)。

不可变 dataclass 值对象与实体快照;价格一律使用 ``Decimal`` 加显式
``currency``,不使用 float。受控枚举(name 与 value 一致)同时被持久层
复用为 VARCHAR + CHECK 的字面值。
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class StorePlatform(str, enum.Enum):
    """店铺所属平台;参考 Agent 仅覆盖淘宝与抖音。"""

    TAOBAO = "TAOBAO"
    DOUYIN = "DOUYIN"


class StoreStatus(str, enum.Enum):
    """店铺可用状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ProductStatus(str, enum.Enum):
    """商品可用状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ListingStatus(str, enum.Enum):
    """渠道上架状态。"""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


@dataclass(frozen=True)
class Price:
    """价格值对象:金额 ``Decimal`` + 显式币种。"""

    amount: Decimal
    currency: str


@dataclass(frozen=True)
class Store:
    """店铺(架构文档 §3 stores)。"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    platform: StorePlatform
    external_store_ref: str
    name: str
    aliases: tuple[str, ...]
    status: StoreStatus
    version: int


@dataclass(frozen=True)
class Product:
    """商品(架构文档 §3 products)。"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    sku: str
    title: str
    status: ProductStatus
    version: int


@dataclass(frozen=True)
class Listing:
    """渠道售卖(架构文档 §3 listings);``version`` 用于乐观并发校验。"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    product_id: uuid.UUID
    external_product_ref: str
    current_price: Price
    cost_price: Price
    status: ListingStatus
    version: int
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PriceLock:
    """锁价窗口(架构文档 §3 price_locks);``[starts_at, ends_at)`` 内禁止改价。"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    listing_id: uuid.UUID
    reason: str
    starts_at: datetime
    ends_at: datetime
    version: int


@dataclass(frozen=True)
class PriceChange:
    """一次改价结果:前后价格与版本,供确认指纹与审计使用。"""

    listing_id: uuid.UUID
    from_price: Price
    to_price: Price
    previous_version: int
    next_version: int


__all__ = [
    "Listing",
    "ListingStatus",
    "Price",
    "PriceChange",
    "PriceLock",
    "Product",
    "ProductStatus",
    "Store",
    "StorePlatform",
    "StoreStatus",
]
