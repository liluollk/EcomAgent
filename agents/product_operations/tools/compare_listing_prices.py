"""``compare_listing_prices`` handler(计划 03 Task 2;架构文档 §4.3)。

READ Tool:对比同一 SKU 在各授权店铺的当前价格与状态。查询始终携带
``workspace_id`` 与授权 Store ID 集合;可选 ``store_ids`` 只允许在授权
范围内收窄,越权以 ``RESOURCE_SCOPE_DENIED`` 拒绝。输出为白名单价格行,
不含 ``cost_price``。
"""

from __future__ import annotations

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.tools.access import (
    authorize_product_read,
    authorized_store_ids,
    ensure_stores_in_scope,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
)
from agents.product_operations.tools.schemas import (
    CompareListingPricesInput,
    CompareListingPricesResult,
    ListingPriceRow,
)
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding


async def compare_listing_prices(
    *,
    actor: ActorContext,
    session: AsyncSession,
    sku: str,
    store_ids: Sequence[str] = (),
) -> CompareListingPricesResult:
    """对比授权店铺内同一 SKU 的价格;workspace_id 过滤恒在。

    未指定 ``store_ids`` 时覆盖全部授权店铺;无任何授权店铺时返回空
    ``items``(拒绝语义 = 不可见)。按店铺 ID 确定性排序。
    """
    authorize_product_read(actor)
    input_model = CompareListingPricesInput(sku=sku, store_ids=tuple(store_ids))
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
            return CompareListingPricesResult(sku=input_model.sku, items=())
        else:
            effective_ids = authorized

    stmt = (
        select(Listing)
        .join_from(Listing, Product, Product.id == Listing.product_id)
        .where(
            Listing.workspace_id == actor.workspace_id,
            Product.workspace_id == actor.workspace_id,
            Product.sku == input_model.sku,
        )
        .order_by(Listing.store_id)
    )
    if effective_ids is not None:
        stmt = stmt.where(
            Listing.store_id.in_([UUID(value) for value in sorted(effective_ids)])
        )

    rows = (await session.scalars(stmt)).all()
    items = tuple(
        ListingPriceRow(
            store_id=str(listing.store_id),
            current_price=listing.current_price,
            currency=listing.currency,
            status=listing.status.value,
            listing_version=listing.version,
        )
        for listing in rows
    )
    return CompareListingPricesResult(sku=input_model.sku, items=items)


COMPARE_LISTING_PRICES_BINDING: Final = ToolBinding(
    definition=COMPARE_LISTING_PRICES_DEFINITION,
    handler=compare_listing_prices,
)


__all__ = [
    "COMPARE_LISTING_PRICES_BINDING",
    "COMPARE_LISTING_PRICES_DEFINITION",
    "compare_listing_prices",
]
