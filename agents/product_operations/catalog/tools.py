"""``search_products`` (P1 v3) 的 ToolDefinition / handler / binding(P1 Task 5)。

与旧版(``tools/definitions.py``,v2)共用 stable key ``search_products``,但输入
换用模型可见引用 ``store_refs``;Registry 支持同一 stable key 多版本并存,旧
Run 仍按旧 Version ID 恢复(Task 1)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.catalog.models import (
    ProductSearchResult,
    SearchProductsInput,
)
from agents.product_operations.catalog.service import CatalogService
from agents.product_operations.input.profiles import p1_fields_for, product_registration
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding
from runtime.capabilities.validation import compute_schema_hash
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_SEARCH_PRODUCTS: Final = "search_products"
SEARCH_PRODUCTS_VERSION: Final = 3


SEARCH_PRODUCTS_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_SEARCH_PRODUCTS,
    version=SEARCH_PRODUCTS_VERSION,
    description=(
        "按关键词(SKU/标题模糊匹配)搜索当前 Workspace 授权店铺内的在售"
        "商品,返回 SKU、商品名与其上架店铺引用;店铺用名称/别名引用,不使用"
        "内部 ID。"
    ),
    input_json_schema=SearchProductsInput.model_json_schema(),
    output_json_schema=ProductSearchResult.model_json_schema(),
    effect=ToolEffect.READ,
    required_permissions=frozenset({"product.read"}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        SearchProductsInput.model_json_schema(), ProductSearchResult.model_json_schema()
    ),
)


async def search_products(
    *,
    actor: ActorContext,
    session: AsyncSession,
    query: str | None = None,
    store_refs: Sequence[str] = (),
) -> ProductSearchResult:
    """经 :class:`CatalogService` 搜索;授权校验与店铺解析都在 Service 内。"""
    return await CatalogService(session).search_products(
        actor=actor, query=query, store_refs=store_refs
    )


SEARCH_PRODUCTS_BINDING: Final = ToolBinding(
    definition=SEARCH_PRODUCTS_DEFINITION,
    handler=search_products,
    pretool_registration=product_registration(
        fields=p1_fields_for(STABLE_KEY_SEARCH_PRODUCTS),
        policy_mode="NONE",
        resource_mode="WORKSPACE",
    ),
)


__all__ = [
    "SEARCH_PRODUCTS_BINDING",
    "SEARCH_PRODUCTS_DEFINITION",
    "SEARCH_PRODUCTS_VERSION",
    "STABLE_KEY_SEARCH_PRODUCTS",
]
