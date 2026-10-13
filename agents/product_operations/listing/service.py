"""Listing 查询应用服务(P1 Task 5;设计 §4.5、§5.2)。

Service 重新解析模型可见的 ``store_ref``(经授权的
:class:`StoreReferenceResolver`)、重新校验读取权限,再以
``workspace_id + store_id + sku`` 查询。未发布与已发布的 Listing 都可读取:
发布状态是读取结果,不是过滤条件(设计 §4.5:"库存为零不等价于取消发布")。
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus, ProductStatus
from agents.product_operations.input.references import StoreReferenceResolver
from agents.product_operations.listing.models import ListingSnapshot
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.shared.errors import NOT_FOUND, ProductOperationError
from agents.product_operations.tools.access import authorize_product_read
from runtime.access.actor import ActorContext


PRODUCT_DISABLED_ISSUE = "PRODUCT_DISABLED"

PublicationState = Literal["PUBLISHED", "UNPUBLISHED", "UNKNOWN"]


def _publication_state(status: ListingStatus) -> PublicationState:
    """受控发布状态;未知取值收敛为 ``UNKNOWN`` 而不是猜测。"""
    if status is ListingStatus.PUBLISHED:
        return "PUBLISHED"
    if status is ListingStatus.UNPUBLISHED:
        return "UNPUBLISHED"
    return "UNKNOWN"


class ListingService:
    """单个 Listing 的只读应用服务。"""

    def __init__(
        self, session: AsyncSession, *, stores: StoreReferenceResolver | None = None
    ) -> None:
        self._session = session
        self._stores = stores or StoreReferenceResolver(session)

    async def get_listing(
        self, *, actor: ActorContext, store_ref: str, sku: str
    ) -> ListingSnapshot:
        """读取授权店铺内单个 SKU 的 Listing 快照。

        店铺引用无法解析 → ``NOT_FOUND`` / ``AMBIGUOUS_STORE_REFERENCE``;
        越权店铺 → ``RESOURCE_SCOPE_DENIED``;店铺内无该 SKU 的上架记录 →
        ``NOT_FOUND``。
        """
        authorize_product_read(actor)
        store_id = await self._stores.resolve_authorized(actor, store_ref)
        stmt = (
            select(Product, Listing)
            .join_from(Product, Listing, Listing.product_id == Product.id)
            .where(
                Product.workspace_id == actor.workspace_id,
                Listing.workspace_id == actor.workspace_id,
                Listing.store_id == store_id,
                Product.sku == sku,
            )
            .order_by(Listing.id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            raise ProductOperationError(
                NOT_FOUND, f"店铺 {store_ref!r} 中不存在 SKU {sku!r} 的上架记录"
            )
        product, listing = row
        issues: list[str] = []
        if product.status is ProductStatus.DISABLED:
            issues.append(PRODUCT_DISABLED_ISSUE)
        return ListingSnapshot(
            store_ref=store_ref,
            sku=product.sku,
            title=product.title,
            current_price=listing.current_price,
            currency=listing.currency,
            publication_state=_publication_state(listing.status),
            listing_version=listing.version,
            issues=tuple(issues),
        )


__all__ = ["PRODUCT_DISABLED_ISSUE", "ListingService", "PublicationState"]
