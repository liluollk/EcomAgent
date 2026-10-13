"""商品域 Repository(计划 03 Task 1)。

约定:
- 只 flush 与条件更新,不提交事务(事务边界归调用方/Runtime);
- 价格写入使用 Listing ``version`` CAS:WHERE 命中零行(版本过期、跨
  Workspace 越权或行不存在)返回 ``None``;
- 读取一律携带 ``workspace_id``,错误 Workspace 不可见。
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.persistence.models import Listing, PriceLock


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
