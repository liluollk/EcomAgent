"""只读商品 Tool 的 ToolDefinition(计划 03 Task 2;架构文档 §4.1-4.3)。

三个 READ Tool 共享 ``product.read`` 权限;Input/Output JSON Schema 由
``schemas.py`` 的 frozen 模型派生,``schema_hash`` 使用 Runtime 的规范化
canonical hash(``compute_schema_hash``),启动校验可复核。
"""

from __future__ import annotations

from typing import Any, Final

from agents.product_operations.tools.schemas import (
    CompareListingPricesInput,
    CompareListingPricesResult,
    GetProductSnapshotInput,
    ProductSearchResult,
    ProductSnapshot,
    SearchProductsInput,
)
from runtime.capabilities.validation import compute_schema_hash
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_SEARCH_PRODUCTS: Final = "search_products"
STABLE_KEY_GET_PRODUCT_SNAPSHOT: Final = "get_product_snapshot"
STABLE_KEY_COMPARE_LISTING_PRICES: Final = "compare_listing_prices"
TOOL_VERSION: Final = 1


def _definition(
    *,
    stable_key: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> ToolDefinition:
    """以派生 Schema 组装 READ ToolDefinition;schema_hash 同步计算。"""
    return ToolDefinition(
        stable_key=stable_key,
        version=TOOL_VERSION,
        description=description,
        input_json_schema=input_schema,
        output_json_schema=output_schema,
        effect=ToolEffect.READ,
        required_permissions=frozenset({"product.read"}),
        source_kind="LOCAL",
        schema_hash=compute_schema_hash(input_schema, output_schema),
    )


SEARCH_PRODUCTS_DEFINITION: Final = _definition(
    stable_key=STABLE_KEY_SEARCH_PRODUCTS,
    description=(
        "按关键词(SKU/标题模糊匹配)搜索当前 Workspace 授权店铺内的在售"
        "商品,返回 SKU、商品名、上架店铺与稳定资源 ID。"
    ),
    input_schema=SearchProductsInput.model_json_schema(),
    output_schema=ProductSearchResult.model_json_schema(),
)

GET_PRODUCT_SNAPSHOT_DEFINITION: Final = _definition(
    stable_key=STABLE_KEY_GET_PRODUCT_SNAPSHOT,
    description=(
        "读取授权店铺内单个 SKU 的商品与 Listing 快照:当前售价、币种、"
        "状态、Listing Version 与脱敏摘要。"
    ),
    input_schema=GetProductSnapshotInput.model_json_schema(),
    output_schema=ProductSnapshot.model_json_schema(),
)

COMPARE_LISTING_PRICES_DEFINITION: Final = _definition(
    stable_key=STABLE_KEY_COMPARE_LISTING_PRICES,
    description="对比同一 SKU 在各授权店铺的当前售价与上架状态。",
    input_schema=CompareListingPricesInput.model_json_schema(),
    output_schema=CompareListingPricesResult.model_json_schema(),
)


__all__ = [
    "COMPARE_LISTING_PRICES_DEFINITION",
    "GET_PRODUCT_SNAPSHOT_DEFINITION",
    "SEARCH_PRODUCTS_DEFINITION",
    "STABLE_KEY_COMPARE_LISTING_PRICES",
    "STABLE_KEY_GET_PRODUCT_SNAPSHOT",
    "STABLE_KEY_SEARCH_PRODUCTS",
    "TOOL_VERSION",
]
