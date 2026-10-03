"""只读商品 Tool 的输入/输出模型(计划 03 Task 2;架构文档 §4.1-4.3)。

frozen pydantic 模型即 Output Schema 白名单:模型未声明的字段
(``cost_price``、``external_product_ref`` 等)既不出现在结果对象,也不
出现在派生的 JSON Schema。JSON Schema 一律由 ``model_json_schema()``
派生,保证 ToolDefinition 与模型一致。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _FrozenModel(BaseModel):
    """frozen 值模型基类:结果对象不可变,只暴露白名单字段。"""

    model_config = ConfigDict(frozen=True)


# ---- 输入模型(同时派生 input JSON Schema) ----


class SearchProductsInput(_FrozenModel):
    """search_products 输入:关键词(匹配 SKU/标题)与可选店铺范围。"""

    query: str | None = None
    store_ids: tuple[str, ...] = ()


class GetProductSnapshotInput(_FrozenModel):
    """get_product_snapshot 输入:店铺 + SKU 定位单个 Listing。"""

    store_id: str
    sku: str


class CompareListingPricesInput(_FrozenModel):
    """compare_listing_prices 输入:SKU 与可选授权店铺子集。"""

    sku: str
    store_ids: tuple[str, ...] = ()


# ---- search_products 输出(SKU、商品名、上架店铺、稳定资源 ID) ----


class ProductStoreRef(_FrozenModel):
    """search_products 输出中的上架店铺引用(白名单:store_id + platform)。"""

    store_id: str
    platform: str


class ProductSearchItem(_FrozenModel):
    """search_products 单个商品条目。"""

    product_id: str
    sku: str
    title: str
    stores: tuple[ProductStoreRef, ...] = ()


class ProductSearchResult(_FrozenModel):
    """search_products 结果。"""

    items: tuple[ProductSearchItem, ...] = ()


# ---- get_product_snapshot 输出(架构文档 §4.2 白名单) ----


class ProductSnapshot(_FrozenModel):
    """单个商品/Listing 快照。

    含当前售价、币种、状态、Listing Version 与脱敏摘要;``cost_price``
    只以 ``redacted_fields`` 摘要形式出现,数值本身不对外输出。
    """

    store_id: str
    product_id: str
    listing_id: str
    sku: str
    title: str
    current_price: Decimal
    currency: str
    status: str
    listing_version: int
    redacted_fields: tuple[str, ...] = ()


# ---- compare_listing_prices 输出(架构文档 §4.3 白名单) ----


class ListingPriceRow(_FrozenModel):
    """compare_listing_prices 单店价格行。"""

    store_id: str
    current_price: Decimal
    currency: str
    status: str
    listing_version: int


class CompareListingPricesResult(_FrozenModel):
    """compare_listing_prices 结果。"""

    sku: str
    items: tuple[ListingPriceRow, ...] = ()


__all__ = [
    "CompareListingPricesInput",
    "CompareListingPricesResult",
    "GetProductSnapshotInput",
    "ListingPriceRow",
    "ProductSearchItem",
    "ProductSearchResult",
    "ProductSnapshot",
    "ProductStoreRef",
    "SearchProductsInput",
]
