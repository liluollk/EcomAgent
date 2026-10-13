"""``search_products`` handler(计划 03 Task 2;架构文档 §4.1)。

READ Tool:按 SKU/标题模糊搜索当前 Workspace 授权店铺内在售商品。查询
始终携带 ``workspace_id`` 与授权 Store ID 集合(``resource_type = store``
的 ResourceScope);输出由 frozen 模型按 Output Schema 白名单构造,
``cost_price`` 等内部字段不进入结果对象。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus, StoreStatus
from agents.product_operations.input.profiles import (
    TOOL_ARGUMENT_FIELDS,
    product_registration,
)
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.tools.access import (
    authorize_product_read,
    authorized_store_ids,
    ensure_stores_in_scope,
)
from agents.product_operations.tools.definitions import SEARCH_PRODUCTS_DEFINITION
from agents.product_operations.tools.schemas import (
    ProductSearchItem,
    ProductSearchResult,
    ProductStoreRef,
    SearchProductsInput,
)
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding


async def search_products(
    *,
    actor: ActorContext,
    session: AsyncSession,
    query: str | None = None,
    store_ids: Sequence[str] = (),
) -> ProductSearchResult:
    """按关键词搜索授权店铺内商品;workspace_id 过滤恒在。

    ``store_ids`` 只允许在授权范围内收窄;越权 store 以
    ``RESOURCE_SCOPE_DENIED`` 拒绝。仅返回 PUBLISHED Listing 与 ACTIVE Store
    ("已发布店铺"语义);按 SKU 与店铺 ID 确定性排序。
    """
    authorize_product_read(actor)
    input_model = SearchProductsInput(query=query, store_ids=tuple(store_ids))
    requested = set(input_model.store_ids)
    ensure_stores_in_scope(actor, tuple(requested))

    effective_ids: AbstractSet[str] | None
    if requested:
        # ensure_stores_in_scope 已保证 requested ⊆ 授权范围。
        effective_ids = requested
    else:
        authorized = authorized_store_ids(actor)
        if authorized is None:
            effective_ids = None  # ALL:当前 Workspace 全部店铺
        elif not authorized:
            return ProductSearchResult(items=())
        else:
            effective_ids = authorized

    stmt = (
        select(Product, Store)
        .join_from(Product, Listing, Listing.product_id == Product.id)
        .join_from(Listing, Store, Store.id == Listing.store_id)
        .where(
            Product.workspace_id == actor.workspace_id,
            Listing.workspace_id == actor.workspace_id,
            Store.workspace_id == actor.workspace_id,
            Listing.status == ListingStatus.PUBLISHED,
            Store.status == StoreStatus.ACTIVE,
        )
        .order_by(Product.sku, Store.id)
    )
    if effective_ids is not None:
        stmt = stmt.where(
            Listing.store_id.in_([UUID(value) for value in sorted(effective_ids)])
        )
    if input_model.query is not None:
        pattern = f"%{input_model.query}%"
        stmt = stmt.where(Product.sku.ilike(pattern) | Product.title.ilike(pattern))

    rows = (await session.execute(stmt)).all()
    products_by_id: dict[UUID, Product] = {}
    stores_by_product: defaultdict[UUID, list[ProductStoreRef]] = defaultdict(list)
    for product, store in rows:
        products_by_id[product.id] = product
        stores_by_product[product.id].append(
            ProductStoreRef(store_id=str(store.id), platform=store.platform.value)
        )
    items = tuple(
        ProductSearchItem(
            product_id=str(product.id),
            sku=product.sku,
            title=product.title,
            stores=tuple(stores_by_product[product.id]),
        )
        for product in products_by_id.values()
    )
    return ProductSearchResult(items=items)


SEARCH_PRODUCTS_BINDING: Final = ToolBinding(
    definition=SEARCH_PRODUCTS_DEFINITION,
    handler=search_products,
    pretool_registration=product_registration(
        fields=TOOL_ARGUMENT_FIELDS["search_products"],
        policy_mode="NONE",
        resource_mode="WORKSPACE",
    ),
)


__all__ = ["SEARCH_PRODUCTS_BINDING", "search_products"]
