"""Catalog 能力模块(P1 Task 5;设计 §4.2)。

只负责商品/SKU 检索:输入/输出模型、只读应用服务与 ``search_products`` 的
Tool 定义与绑定。不包含价格、库存或刊登职责。
"""

from __future__ import annotations

from agents.product_operations.catalog.models import (
    ProductSearchItem,
    ProductSearchResult,
    ProductStoreRef,
    SearchProductsInput,
)
from agents.product_operations.catalog.service import CatalogService
from agents.product_operations.catalog.tools import (
    SEARCH_PRODUCTS_BINDING,
    SEARCH_PRODUCTS_DEFINITION,
    SEARCH_PRODUCTS_VERSION,
    STABLE_KEY_SEARCH_PRODUCTS,
)


__all__ = [
    "SEARCH_PRODUCTS_BINDING",
    "SEARCH_PRODUCTS_DEFINITION",
    "SEARCH_PRODUCTS_VERSION",
    "STABLE_KEY_SEARCH_PRODUCTS",
    "CatalogService",
    "ProductSearchItem",
    "ProductSearchResult",
    "ProductStoreRef",
    "SearchProductsInput",
]
