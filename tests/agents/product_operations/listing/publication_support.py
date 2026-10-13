"""Listing 发布能力测试共享支撑(P1 Task 8)。

放在独立模块而非 ``conftest.py``:conftest 的模块名在多层嵌套下不唯一,直接
``import conftest`` 不可靠;而 pytest 的 prepend 导入模式与本项目 mypy 均会把测试
文件所在目录加入搜索路径,因此 ``import publication_support`` 对运行期与类型检查
都成立。

模块名刻意**不**叫 ``_support``:prepend 导入模式按**文件基名**缓存模块,兄弟目录
(``tests/agents/product_operations/inventory``)已有一个 ``_support``;同一次 pytest
调用收集两个目录时,后者只能拿到前者的模块对象(ImportError),这正是本仓库既有
``inventory/_support.py`` 文档里"conftest 模块名不唯一"问题的同型变体。这里用唯一
基名规避,不改动既有测试。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus, StorePlatform
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingGateway,
    ListingPublicationSnapshot,
    ListingQuery,
    ReconcileListingCommand,
)
from agents.product_operations.listing.policy import ListingPolicyConfig
from agents.product_operations.listing.service import (
    PublishListingHandler,
    UnpublishListingHandler,
)
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    InventoryLevel,
    Listing,
    Product,
    Store,
)
from agents.product_operations.shared.write_contracts import WriteReceipt
from runtime.access.actor import ActorContext
from runtime.execution.reconciliation import ReconcileResult
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


PUBLICATION_POLICY_CONFIG = ListingPolicyConfig(policy_version="listing-policy-v1")

DEFAULT_STORE_NAME = "旗舰店"
DEFAULT_SKU = "SKU-1"
DEFAULT_QUANTITY = 20


class RecordingListingGateway:
    """记录写命令并返回固定回执的测试 Gateway(ListingGateway 协议)。

    只用于断言"Handler 把哪些字段交给了平台";发布方向由调用的方法表达,因此两个
    写方法都要记录,否则无法发现方向被写错。
    """

    def __init__(self, receipt: WriteReceipt | None = None) -> None:
        self.command: ChangePublicationCommand | None = None
        self.direction: str | None = None
        self._receipt = receipt

    async def get_listing(self, query: ListingQuery) -> ListingPublicationSnapshot:
        raise NotImplementedError  # pragma: no cover - 本替身只覆盖写路径

    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        self.command = command
        self.direction = "publish"
        assert self._receipt is not None
        return self._receipt

    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        self.command = command
        self.direction = "unpublish"
        assert self._receipt is not None
        return self._receipt

    async def reconcile_listing(  # pragma: no cover - 本替身只覆盖写路径
        self, command: ReconcileListingCommand
    ) -> ReconcileResult:
        raise NotImplementedError


def make_actor(
    workspace_id: uuid.UUID,
    *,
    store_ids: Sequence[uuid.UUID] | None = None,
    permissions: frozenset[str] = frozenset(
        {"product.read", "product.listing.write"}
    ),
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
        request_id="req-listing-publication",
    )


@dataclass(frozen=True)
class PublicationEnv:
    """一处已提交的刊登种子与请求级协作对象。"""

    workspace_id: uuid.UUID
    store: Store
    product: Product
    listing: Listing
    location: FulfillmentLocation
    level: InventoryLevel
    actor: ActorContext
    session: AsyncSession

    def make_publish_handler(
        self,
        gateway: ListingGateway,
        *,
        actor: ActorContext | None = None,
        policy_config: ListingPolicyConfig = PUBLICATION_POLICY_CONFIG,
    ) -> PublishListingHandler:
        """构造发布写 Handler;``actor`` 省略时用 env 的默认授权 Actor。

        ``gateway`` 取 Port 类型:测试既注入 :class:`RecordingListingGateway`
        (只验证命令装配),也注入真实的 ``MockCommerceAdapter``(验证端到端语义)。
        """
        return PublishListingHandler(
            actor=actor or self.actor,
            session=self.session,
            gateway=gateway,
            policy_config=policy_config,
        )

    def make_unpublish_handler(
        self,
        gateway: ListingGateway,
        *,
        actor: ActorContext | None = None,
        policy_config: ListingPolicyConfig = PUBLICATION_POLICY_CONFIG,
    ) -> UnpublishListingHandler:
        """构造取消发布写 Handler;``actor`` 省略时用 env 的默认授权 Actor。"""
        return UnpublishListingHandler(
            actor=actor or self.actor,
            session=self.session,
            gateway=gateway,
            policy_config=policy_config,
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


async def seed_publication(
    session: AsyncSession,
    *,
    status: ListingStatus = ListingStatus.PUBLISHED,
    quantity: int = DEFAULT_QUANTITY,
    sku: str = DEFAULT_SKU,
    store_name: str = DEFAULT_STORE_NAME,
) -> tuple[Workspace, Store, Product, Listing, FulfillmentLocation, InventoryLevel]:
    """创建独立 Workspace + Store/Product/Listing/位置/库存并 flush(不提交)。

    默认种子的 Listing 已是 ``PUBLISHED``,``reconcile`` 的目标状态因此可以取
    "与当前相同"(幂等 no-op)或"与当前相反"(真实迁移)两种对照面。
    """
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
        status=status,
    )
    location = FulfillmentLocation(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=store.id,
        external_location_ref=f"loc-{uuid.uuid4()}",
        name="默认仓",
        aliases=["默认仓"],
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


__all__ = [
    "CLEANUP_STATEMENTS",
    "DEFAULT_QUANTITY",
    "DEFAULT_SKU",
    "DEFAULT_STORE_NAME",
    "PUBLICATION_POLICY_CONFIG",
    "PublicationEnv",
    "RecordingListingGateway",
    "cleanup_workspace",
    "make_actor",
    "seed_publication",
]
