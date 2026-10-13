"""商品域 Repository(计划 03 Task 1;P1 Task 4 扩展库存与发布状态)。

约定:
- 只 flush 与条件更新,不提交事务(事务边界归调用方/Runtime);
- 写入使用行 ``version`` CAS:WHERE 命中零行(版本过期、跨 Workspace 越权或行
  不存在)返回 ``None``;
- 读取一律携带 ``workspace_id``,错误 Workspace 不可见;
- 库存 CAS 额外保证 ``available_quantity + delta >= 0``,负结果不命中而不报错。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.persistence.models import (
    InventoryLevel,
    Listing,
    PriceLock,
)


class ProductRepository:
    """stores/products/listings/price_locks 的最小持久化接口。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_listing(
        self, workspace_id: uuid.UUID, listing_id: uuid.UUID
    ) -> Listing | None:
        """按 (workspace_id, listing_id) 读取;错误 Workspace 返回 None。

        ``populate_existing`` 绕过身份映射,避免 CAS 后读到过期的 ``version``。
        """
        stmt = (
            select(Listing)
            .where(Listing.workspace_id == workspace_id, Listing.id == listing_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def update_price(
        self,
        *,
        workspace_id: uuid.UUID,
        listing_id: uuid.UUID,
        target_price: Decimal,
        expected_version: int,
    ) -> Listing | None:
        """CAS 改价(架构文档 §7:以 expected_version 乐观并发写入)。

        单条 ``UPDATE ... WHERE version = expected_version RETURNING Listing``;
        返回更新后的行,零行命中(版本过期/不存在/越权)返回 ``None``。

        ``updated_at`` 显式使用 ``clock_timestamp()``(语句时间):``now()``
        是事务开始时间,同事务内先 INSERT 后 UPDATE 会得到相同值,无法观察
        变化;``clock_timestamp()`` 保证每次写入都可区分。
        """
        stmt = (
            sa.update(Listing)
            .where(
                Listing.id == listing_id,
                Listing.workspace_id == workspace_id,
                Listing.version == expected_version,
            )
            .values(
                current_price=target_price,
                version=Listing.version + 1,
                updated_at=sa.text("clock_timestamp()"),
            )
            .returning(Listing)
        )
        updated: Listing | None = (await self._session.scalars(stmt)).one_or_none()
        return updated

    async def adjust_inventory(
        self,
        *,
        workspace_id: uuid.UUID,
        inventory_level_id: uuid.UUID,
        delta: int,
        expected_version: int,
    ) -> InventoryLevel | None:
        """增量调整库存(P1 设计 §7);CAS + 非负双重条件。

        WHERE 同时包含 workspace、ID、``version = expected_version`` 与
        ``available_quantity + delta >= 0``:版本过期、跨 Workspace、行不存在或
        调整后为负都不命中,返回 ``None``(不抛异常,由上层判定失败语义)。
        """
        stmt = (
            sa.update(InventoryLevel)
            .where(
                InventoryLevel.id == inventory_level_id,
                InventoryLevel.workspace_id == workspace_id,
                InventoryLevel.version == expected_version,
                InventoryLevel.available_quantity + delta >= 0,
            )
            .values(
                available_quantity=InventoryLevel.available_quantity + delta,
                version=InventoryLevel.version + 1,
                updated_at=sa.text("clock_timestamp()"),
            )
            .returning(InventoryLevel)
        )
        adjusted: InventoryLevel | None = (await self._session.scalars(stmt)).one_or_none()
        return adjusted

    async def set_listing_publication_state(
        self,
        *,
        workspace_id: uuid.UUID,
        listing_id: uuid.UUID,
        target_state: ListingStatus,
        expected_version: int,
    ) -> Listing | None:
        """CAS 迁移 Listing 发布状态(P1 设计 §5.3/§7)。

        ``target_state`` 是服务端已校验的目标状态;WHERE 命中零行(版本过期、
        跨 Workspace 或行不存在)返回 ``None``。
        """
        stmt = (
            sa.update(Listing)
            .where(
                Listing.id == listing_id,
                Listing.workspace_id == workspace_id,
                Listing.version == expected_version,
            )
            .values(
                status=target_state,
                version=Listing.version + 1,
                updated_at=sa.text("clock_timestamp()"),
            )
            .returning(Listing)
        )
        updated: Listing | None = (await self._session.scalars(stmt)).one_or_none()
        return updated

    async def get_active_price_lock(
        self,
        workspace_id: uuid.UUID,
        listing_id: uuid.UUID,
        *,
        at: datetime | None = None,
    ) -> PriceLock | None:
        """查询指定时刻的活动锁(``starts_at <= at < ends_at``)。

        ``at`` 缺省取当前 UTC 时间;跨 Workspace 返回 None。
        """
        effective_at = at if at is not None else datetime.now(tz=UTC)
        stmt = (
            select(PriceLock)
            .where(
                PriceLock.workspace_id == workspace_id,
                PriceLock.listing_id == listing_id,
                PriceLock.starts_at <= effective_at,
                PriceLock.ends_at > effective_at,
            )
            .order_by(PriceLock.starts_at.desc())
            .limit(1)
        )
        return (await self._session.scalars(stmt)).one_or_none()


__all__ = ["ProductRepository"]
