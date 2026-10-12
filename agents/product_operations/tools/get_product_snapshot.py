"""``get_product_snapshot`` handler(计划 03 Task 2;架构文档 §4.2)。

READ Tool:按 (store_id, sku) 读取单个商品/Listing 快照。先经 Runtime
``Authorizer`` 校验 ``product.read`` 与 store 资源范围,再以
``workspace_id + store_id + sku`` 查询;输出为白名单字段,cost_price 数值
不输出,只给出脱敏摘要。
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import (
    TOOL_ARGUMENT_FIELDS,
    product_registration,
)
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.policy.resources import StoreResourceResolver
from agents.product_operations.tools.access import authorize_store
from agents.product_operations.tools.definitions import GET_PRODUCT_SNAPSHOT_DEFINITION
from agents.product_operations.tools.errors import (
    ProductNotFoundError,
    ToolAccessError,
)
from agents.product_operations.tools.schemas import (
    GetProductSnapshotInput,
    ProductSnapshot,
)
from runtime.access.actor import ActorContext
from runtime.access.authorization import RESOURCE_SCOPE_DENIED
from runtime.capabilities.registry import ToolBinding


REDACTED_FIELDS: Final = ("cost_price",)


async def get_product_snapshot(
    *,
    actor: ActorContext,
    session: AsyncSession,
    store_id: str,
    sku: str,
) -> ProductSnapshot:
    """读取授权店铺内单个 SKU 的快照;始终携带 workspace_id 过滤。

    未授权 store 以 ``RESOURCE_SCOPE_DENIED`` 拒绝;店铺内无该 SKU 的
    上架记录时抛出 ``ProductNotFoundError``。
    """
    authorize_store(actor, store_id=store_id)
    input_model = GetProductSnapshotInput(store_id=store_id, sku=sku)
    store_uuid = _as_store_uuid(input_model.store_id)

    stmt = (
        select(Product, Listing)
        .join_from(Product, Listing, Listing.product_id == Product.id)
        .where(
            Product.workspace_id == actor.workspace_id,
            Listing.workspace_id == actor.workspace_id,
            Listing.store_id == store_uuid,
            Product.sku == input_model.sku,
        )
        .order_by(Listing.id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise ProductNotFoundError(
            f"store {input_model.store_id} 中不存在 SKU {input_model.sku} 的上架记录"
        )
    product, listing = row
    return ProductSnapshot(
        store_id=input_model.store_id,
        product_id=str(product.id),
        listing_id=str(listing.id),
        sku=product.sku,
        title=product.title,
        current_price=listing.current_price,
        currency=listing.currency,
        status=listing.status.value,
        listing_version=listing.version,
        redacted_fields=REDACTED_FIELDS,
    )


def _as_store_uuid(store_id: str) -> UUID:
    """解析店铺 UUID;非法引用按越权拒绝,不做存在性探测。"""
    try:
        return UUID(store_id)
    except ValueError as error:
        raise ToolAccessError(
            RESOURCE_SCOPE_DENIED,
            f"store {store_id} 不是合法的店铺引用",
        ) from error


GET_PRODUCT_SNAPSHOT_BINDING: Final = ToolBinding(
    definition=GET_PRODUCT_SNAPSHOT_DEFINITION,
    handler=get_product_snapshot,
    pretool_registration=product_registration(
        fields=TOOL_ARGUMENT_FIELDS["get_product_snapshot"],
        policy_mode="NONE",
        resource_mode="RESOURCE",
        resource_resolver=StoreResourceResolver(),
    ),
)


__all__ = ["GET_PRODUCT_SNAPSHOT_BINDING", "get_product_snapshot"]
