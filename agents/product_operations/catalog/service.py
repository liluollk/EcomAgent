"""Catalog 应用服务(P1 Task 5;设计 §4.2、§4.1)。

职责边界:PreToolUse 只做预检;本 Service 在任何查询前重新解析模型可见的
``store_refs``(经授权的 :class:`StoreReferenceResolver`)并重新校验读取权限,
再以 ``workspace_id + 授权 store_id`` 过滤执行查询。模型无法通过参数影响
内部 ``store_id`` 的取值。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.catalog.models import (
    ProductSearchItem,
    ProductSearchResult,
    ProductStoreRef,
)
from agents.product_operations.domain.models import ListingStatus, StoreStatus
from agents.product_operations.input.references import StoreReferenceResolver
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.tools.access import authorize_product_read
from runtime.access.actor import ActorContext


class CatalogService:
    """商品/SKU 检索的只读应用服务。"""

    def __init__(
        self, session: AsyncSession, *, stores: StoreReferenceResolver | None = None
    ) -> None:
        self._session = session
        self._stores = stores or StoreReferenceResolver(session)

    async def search_products(
        self,
        *,
        actor: ActorContext,
        query: str | None = None,
        store_refs: Sequence[str] = (),
    ) -> ProductSearchResult:
        """按关键词搜索授权店铺内的在售商品。

        仅在授权、ACTIVE 且已发布(PUBLISHED)的 Listing 上匹配;输出不包含
        成本价、平台凭证或内部 UUID。``store_refs`` 为空表示全部授权店铺;
        无任何授权店铺时返回空结果,不静默放宽为全量。
        """
        authorize_product_read(actor)
        store_ids = await self._stores.resolve_authorized_many(actor, store_refs)
        if not store_ids:
            return ProductSearchResult(items=())
        return await self._search(
            workspace_id=actor.workspace_id, query=query, store_ids=store_ids
        )

    async def _search(
        self,
        *,
        workspace_id: UUID,
        query: str | None,
        store_ids: Sequence[UUID],
    ) -> ProductSearchResult:
        stmt = (
            select(Product, Store)
            .join_from(Product, Listing, Listing.product_id == Product.id)
            .join_from(Listing, Store, Store.id == Listing.store_id)
            .where(
                Product.workspace_id == workspace_id,
                Listing.workspace_id == workspace_id,
                Store.workspace_id == workspace_id,
                Listing.status == ListingStatus.PUBLISHED,
                Store.status == StoreStatus.ACTIVE,
                Listing.store_id.in_(sorted(store_ids)),
            )
            .order_by(Product.sku, Store.name, Store.id)
        )
        if query is not None:
            pattern = f"%{query}%"
            stmt = stmt.where(Product.sku.ilike(pattern) | Product.title.ilike(pattern))

        rows = (await self._session.execute(stmt)).all()
        products: dict[UUID, Product] = {}
        stores_by_product: defaultdict[UUID, list[ProductStoreRef]] = defaultdict(list)
        for product, store in rows:
            products[product.id] = product
            stores_by_product[product.id].append(
                ProductStoreRef(store_ref=store.name, platform=store.platform.value)
            )
        items = [
            ProductSearchItem(
                sku=product.sku,
                title=product.title,
                stores=tuple(stores_by_product[product_id]),
            )
            for product_id, product in products.items()
        ]
        items.sort(key=lambda item: item.sku)
        return ProductSearchResult(items=tuple(items))


__all__ = ["CatalogService"]
