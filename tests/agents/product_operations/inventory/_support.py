"""Inventory 测试共享支撑(P1 Task 7)。

放在独立模块而非 ``conftest.py``:conftest 的模块名在多层嵌套下不唯一,直接
``import conftest`` 不可靠;而 pytest 的 prepend 导入模式与本项目 mypy 均会把
测试文件所在目录加入搜索路径,因此 ``import _support`` 对运行期与类型检查都成立。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    InventoryLevelSnapshot,
    InventoryQuery,
    InventoryWriteReceipt,
    ReconcileInventoryCommand,
)
from agents.product_operations.inventory.policy import InventoryPolicyConfig
from agents.product_operations.inventory.service import (
    AdjustInventoryQuantityHandler,
    InventoryService,
)
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    InventoryLevel,
    Listing,
    Product,
    Store,
)
from runtime.access.actor import ActorContext
from runtime.execution.reconciliation import ReconcileResult
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


INVENTORY_POLICY_CONFIG = InventoryPolicyConfig(policy_version="inventory-policy-v1")

DEFAULT_STORE_NAME = "旗舰店"
DEFAULT_LOCATION_NAME = "默认仓"
DEFAULT_SKU = "SKU-1"
DEFAULT_QUANTITY = 20


class RecordingInventoryGateway:
    """记录写命令并返回固定回执的测试 Gateway(InventoryGateway 协议)。"""

    def __init__(self, receipt: InventoryWriteReceipt | None = None) -> None:
        self.command: AdjustInventoryCommand | None = None
        self._receipt = receipt

    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot:
        raise NotImplementedError  # pragma: no cover

    async def adjust_inventory(
        self, command: AdjustInventoryCommand
    ) -> InventoryWriteReceipt:
        self.command = command
        assert self._receipt is not None
        return self._receipt

    async def reconcile_inventory(  # pragma: no cover
        self, command: ReconcileInventoryCommand
    ) -> ReconcileResult:
        raise NotImplementedError


def make_actor(
    workspace_id: uuid.UUID,
    *,
    store_ids: Sequence[uuid.UUID] | None = None,
    permissions: frozenset[str] = frozenset({"product.read", "product.inventory.write"}),
) -> ActorContext:
    """构造商品 Actor;``store_ids`` 省略表示 ``ALL``,否则为 ``SELECTED``。"""
    scope = (
        ResourceScope(mode="ALL")
        if store_ids is None
        else ResourceScope(mode="SELECTED", resource_ids={str(s) for s in store_ids})
    )
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=permissions,
        resource_scopes={"store": scope},
        request_id="req-inventory",
    )


@dataclass(frozen=True)
class InventoryEnv:
    """一处已提交的库存种子与请求级协作对象。"""

    workspace_id: uuid.UUID
    store: Store
    product: Product
    listing: Listing
    location: FulfillmentLocation
    level: InventoryLevel
    actor: ActorContext
    session: AsyncSession
    service: InventoryService

    def make_handler(
        self, gateway: RecordingInventoryGateway, *, actor: ActorContext | None = None
    ) -> AdjustInventoryQuantityHandler:
        """构造写 Handler;``actor`` 省略时用 env 的默认授权 Actor。"""
        return AdjustInventoryQuantityHandler(
            actor=actor or self.actor,
            session=self.session,
            gateway=gateway,
            policy_config=INVENTORY_POLICY_CONFIG,
        )


CLEANUP_STATEMENTS: tuple[str, ...] = (
    "DELETE FROM product_operations.inventory_levels WHERE workspace_id = :ws",
    "DELETE FROM product_operations.fulfillment_locations WHERE workspace_id = :ws",
    "DELETE FROM product_operations.listings WHERE workspace_id = :ws",
    "DELETE FROM product_operations.products WHERE workspace_id = :ws",
    "DELETE FROM product_operations.stores WHERE workspace_id = :ws",
    "DELETE FROM workspaces WHERE id = :ws",
)


async def cleanup_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """按外键倒序删除用例种子行并提交。"""
    for statement in CLEANUP_STATEMENTS:
        await session.execute(sa.text(statement), {"ws": workspace_id})
    await session.commit()


async def seed_inventory(
    session: AsyncSession,
    *,
    quantity: int = DEFAULT_QUANTITY,
    store_name: str = DEFAULT_STORE_NAME,
    location_name: str = DEFAULT_LOCATION_NAME,
    sku: str = DEFAULT_SKU,
) -> tuple[Workspace, Store, Product, Listing, FulfillmentLocation, InventoryLevel]:
    """创建独立 Workspace + Store/Product/Listing/位置/库存并 flush(不提交)。"""
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    store = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"shop-{uuid.uuid4()}",
        name=store_name,
        aliases=[store_name],
    )
    product = Product(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        sku=sku,
        title="轻量保温杯 500ml",
    )
    session.add(workspace)
    await session.flush()
    session.add_all((store, product))
    await session.flush()
    listing = Listing(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=store.id,
        product_id=product.id,
        external_product_ref="item-1",
        currency="CNY",
        current_price=Decimal("128.00"),
        cost_price=Decimal("88.00"),
    )
    location = FulfillmentLocation(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=store.id,
        external_location_ref=f"loc-{uuid.uuid4()}",
        name=location_name,
        aliases=[location_name],
    )
    session.add_all((listing, location))
    await session.flush()
    level = InventoryLevel(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        location_id=location.id,
        product_id=product.id,
        available_quantity=quantity,
    )
    session.add(level)
    await session.flush()
    await session.refresh(level)
    return workspace, store, product, listing, location, level


async def add_active_location(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    store_id: uuid.UUID,
    *,
    name: str,
) -> FulfillmentLocation:
    """在同一店铺下追加一个启用位置(用于多位置歧义场景)。"""
    location = FulfillmentLocation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        store_id=store_id,
        external_location_ref=f"loc-{uuid.uuid4()}",
        name=name,
        aliases=[name],
    )
    session.add(location)
    await session.flush()
    return location


__all__ = [
    "CLEANUP_STATEMENTS",
    "DEFAULT_LOCATION_NAME",
    "DEFAULT_QUANTITY",
    "DEFAULT_SKU",
    "DEFAULT_STORE_NAME",
    "INVENTORY_POLICY_CONFIG",
    "InventoryEnv",
    "RecordingInventoryGateway",
    "add_active_location",
    "cleanup_workspace",
    "make_actor",
    "seed_inventory",
]
