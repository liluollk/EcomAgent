"""商品域 Schema 与 Repository 测试(计划 03 Task 1;P1 Task 4 扩展库存位置)。

覆盖:
- Listing version CAS(过期版本拒绝、正确版本成功、version 递增);
- get_listing 按 workspace 隔离;
- PriceLock 活动锁窗口查询与"同 Listing 至多一个未过期锁"唯一性;
- Store platform 枚举只允许 TAOBAO/DOUYIN;
- Numeric(18,2) 价格 Decimal 往返无损;
- 架构文档 §3 的三个同 Workspace 唯一约束;
- 领域模型不可变;
- P1:ListingStatus 只允许 PUBLISHED/UNPUBLISHED;
- P1:FulfillmentLocation/InventoryLevel 唯一约束与非负库存 CHECK;
- P1:adjust_inventory 与 set_listing_publication_state 的 CAS 语义。
"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain import models as domain
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    InventoryLevel,
    Listing,
    PriceLock,
    Product,
    Store,
    StorePlatform,
)
from agents.product_operations.persistence.repository import ProductRepository


async def test_listing_compare_and_set_rejects_stale_version(
    product_repository: ProductRepository, listing: Listing
) -> None:
    updated = await product_repository.update_price(
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        target_price=Decimal("99.00"),
        expected_version=listing.version + 1,
    )
    assert updated is None


async def test_update_price_cas_succeeds_returns_updated_row(
    product_repository: ProductRepository, listing: Listing
) -> None:
    """正确 version 的 CAS 返回更新后的行:version+1、新价格、updated_at 变化。"""
    previous_version = listing.version
    previous_updated_at = listing.updated_at

    updated = await product_repository.update_price(
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        target_price=Decimal("99.00"),
        expected_version=previous_version,
    )

    assert updated is not None
    assert updated.id == listing.id
    assert updated.current_price == Decimal("99.00")
    assert updated.currency == listing.currency
    assert updated.version == previous_version + 1
    assert updated.updated_at > previous_updated_at


async def test_listing_version_increments_across_successive_updates(
    product_repository: ProductRepository, listing: Listing
) -> None:
    row = listing
    version = listing.version
    for target_price in (Decimal("99.00"), Decimal("98.00"), Decimal("97.00")):
        row = await product_repository.update_price(
            workspace_id=listing.workspace_id,
            listing_id=listing.id,
            target_price=target_price,
            expected_version=version,
        )
        assert row is not None
        assert row.version == version + 1
        assert row.current_price == target_price
        version += 1


async def test_get_listing_scoped_to_workspace(
    product_repository: ProductRepository, listing: Listing
) -> None:
    found = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert found is not None
    assert found.id == listing.id

    assert await product_repository.get_listing(uuid.uuid4(), listing.id) is None


async def test_active_price_lock_query_respects_window(
    product_repository: ProductRepository, product_seed
) -> None:
    lock = product_seed.price_lock
    listing = product_seed.listing

    active = await product_repository.get_active_price_lock(
        listing.workspace_id, listing.id, at=lock.starts_at + timedelta(minutes=1)
    )
    assert active is not None
    assert active.id == lock.id

    assert (
        await product_repository.get_active_price_lock(
            listing.workspace_id, listing.id, at=lock.starts_at - timedelta(seconds=1)
        )
        is None
    )
    # ends_at 为排他边界:结束时刻之后锁不再生效。
    assert (
        await product_repository.get_active_price_lock(
            listing.workspace_id, listing.id, at=lock.ends_at
        )
        is None
    )
    assert (
        await product_repository.get_active_price_lock(uuid.uuid4(), listing.id, at=lock.starts_at)
        is None
    )


async def test_only_one_active_price_lock_per_listing(
    product_seed, db_session: AsyncSession
) -> None:
    """未过期锁按 (workspace_id, listing_id) 唯一;过期锁不受限制。"""
    now = datetime.now(tz=UTC)
    duplicate = PriceLock(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        listing_id=product_seed.listing.id,
        reason="duplicate-active-lock",
        starts_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(days=1),
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()

    expired = PriceLock(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        listing_id=product_seed.listing.id,
        reason="expired-lock",
        starts_at=now - timedelta(days=2),
        ends_at=now - timedelta(days=1),
    )
    db_session.add(expired)
    await db_session.flush()


def test_store_platform_enum_only_allows_taobao_and_douyin() -> None:
    assert {member.value for member in StorePlatform} == {"TAOBAO", "DOUYIN"}


async def test_store_platform_check_constraint_rejects_other_platform(
    product_seed, db_session: AsyncSession
) -> None:
    """绕过客户端枚举校验,直接插入未知 platform,由数据库 CHECK 拒绝。"""
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                sa.text(
                    "INSERT INTO product_operations.stores "
                    "(id, workspace_id, platform, external_store_ref, name, aliases, "
                    "status, version) "
                    "VALUES (:id, :workspace_id, 'PDD', 'shop-x', 'n', '[]'::jsonb, "
                    "'ACTIVE', 1)"
                ),
                {"id": uuid.uuid4(), "workspace_id": product_seed.workspace.id},
            )


async def test_price_numeric_roundtrip_is_lossless(
    product_repository: ProductRepository, listing: Listing, db_session: AsyncSession
) -> None:
    updated = await product_repository.update_price(
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        target_price=Decimal("99.00"),
        expected_version=listing.version,
    )
    assert updated is not None

    reloaded = await db_session.scalar(sa.select(Listing).where(Listing.id == listing.id))
    assert reloaded is not None
    assert isinstance(reloaded.current_price, Decimal)
    assert reloaded.current_price == Decimal("99.00")
    assert str(reloaded.current_price) == "99.00"
    assert isinstance(reloaded.cost_price, Decimal)
    assert reloaded.cost_price == Decimal("88.00")


async def test_product_sku_unique_within_workspace(
    product_seed, db_session: AsyncSession
) -> None:
    duplicate = Product(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        sku=product_seed.product.sku,
        title="duplicate-sku",
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()


async def test_store_unique_within_workspace(product_seed, db_session: AsyncSession) -> None:
    duplicate = Store(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        platform=product_seed.store.platform,
        external_store_ref=product_seed.store.external_store_ref,
        name="duplicate-store",
        aliases=[],
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()


async def test_listing_unique_within_workspace(
    product_seed, db_session: AsyncSession
) -> None:
    duplicate = Listing(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        store_id=product_seed.store.id,
        product_id=product_seed.product.id,
        external_product_ref="item-duplicate",
        currency="CNY",
        current_price=Decimal("100.00"),
        cost_price=Decimal("88.00"),
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()


def test_domain_models_are_frozen() -> None:
    price = domain.Price(amount=Decimal("99.00"), currency="CNY")
    store = domain.Store(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        platform=domain.StorePlatform.TAOBAO,
        external_store_ref="shop-1",
        name="store",
        aliases=("taobao",),
        status=domain.StoreStatus.ACTIVE,
        version=1,
    )
    product = domain.Product(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        sku="SKU-001",
        title="product",
        status=domain.ProductStatus.ACTIVE,
        version=1,
    )
    listing = domain.Listing(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        store_id=store.id,
        product_id=product.id,
        external_product_ref="item-1",
        current_price=price,
        cost_price=domain.Price(amount=Decimal("88.00"), currency="CNY"),
        status=domain.ListingStatus.PUBLISHED,
        version=1,
    )
    lock = domain.PriceLock(
        id=uuid.uuid4(),
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        reason="promo",
        starts_at=datetime.now(tz=UTC),
        ends_at=datetime.now(tz=UTC) + timedelta(days=1),
        version=1,
    )
    change = domain.PriceChange(
        listing_id=listing.id,
        from_price=listing.current_price,
        to_price=domain.Price(amount=Decimal("98.00"), currency="CNY"),
        previous_version=listing.version,
        next_version=listing.version + 1,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        store.name = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        price.amount = Decimal("1.00")
    with pytest.raises(dataclasses.FrozenInstanceError):
        listing.version = 2

    assert change.previous_version == 1
    assert change.to_price.amount == Decimal("98.00")
    assert lock.reason == "promo"


# ---- P1 Task 4:库存位置模型与 Listing 发布状态 ----


def test_listing_status_enum_only_allows_publication_states() -> None:
    """第一版 ACTIVE/INACTIVE 已迁移为 PUBLISHED/UNPUBLISHED(P1 设计 §7)。"""
    assert set(domain.ListingStatus) == {
        domain.ListingStatus.PUBLISHED,
        domain.ListingStatus.UNPUBLISHED,
    }


async def test_seed_inventory_level_defaults(product_seed) -> None:
    assert product_seed.inventory_level.available_quantity == 20
    assert product_seed.inventory_level.version == 1
    assert product_seed.inventory_level.location_id == product_seed.fulfillment_location.id
    assert product_seed.inventory_level.product_id == product_seed.product.id


async def test_fulfillment_location_unique_within_workspace(
    product_seed, db_session: AsyncSession
) -> None:
    duplicate = FulfillmentLocation(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        store_id=product_seed.store.id,
        external_location_ref=product_seed.fulfillment_location.external_location_ref,
        name="duplicate-location",
        aliases=[],
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()


async def test_inventory_level_unique_within_workspace(
    product_seed, db_session: AsyncSession
) -> None:
    duplicate = InventoryLevel(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        location_id=product_seed.fulfillment_location.id,
        product_id=product_seed.product.id,
        available_quantity=5,
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()


async def test_inventory_level_rejects_negative_quantity(
    product_seed, db_session: AsyncSession
) -> None:
    """绕过应用层校验直接写入负库存,由数据库 CHECK 拒绝。"""
    negative = InventoryLevel(
        id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        location_id=product_seed.fulfillment_location.id,
        product_id=uuid.uuid4(),
        available_quantity=-1,
    )
    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(negative)
            await db_session.flush()


async def test_adjust_inventory_cas_updates_quantity_and_version(
    product_repository: ProductRepository, product_seed
) -> None:
    level = product_seed.inventory_level
    previous_version = level.version
    updated = await product_repository.adjust_inventory(
        workspace_id=level.workspace_id,
        inventory_level_id=level.id,
        delta=-5,
        expected_version=previous_version,
    )
    assert updated is not None
    assert updated.id == level.id
    assert updated.available_quantity == 15
    assert updated.version == previous_version + 1


async def test_adjust_inventory_rejects_stale_version(
    product_repository: ProductRepository, product_seed
) -> None:
    level = product_seed.inventory_level
    updated = await product_repository.adjust_inventory(
        workspace_id=level.workspace_id,
        inventory_level_id=level.id,
        delta=-5,
        expected_version=level.version + 1,
    )
    assert updated is None


async def test_adjust_inventory_rejects_negative_result(
    product_repository: ProductRepository, product_seed
) -> None:
    """调整后为负时 SQL 侧不命中(available_quantity + delta >= 0),返回 None。"""
    level = product_seed.inventory_level
    updated = await product_repository.adjust_inventory(
        workspace_id=level.workspace_id,
        inventory_level_id=level.id,
        delta=-21,
        expected_version=level.version,
    )
    assert updated is None


async def test_adjust_inventory_scoped_to_workspace(
    product_repository: ProductRepository, product_seed
) -> None:
    level = product_seed.inventory_level
    updated = await product_repository.adjust_inventory(
        workspace_id=uuid.uuid4(),
        inventory_level_id=level.id,
        delta=-5,
        expected_version=level.version,
    )
    assert updated is None


async def test_set_listing_publication_state_cas(
    product_repository: ProductRepository, product_seed
) -> None:
    listing = product_seed.listing
    previous_version = listing.version
    updated = await product_repository.set_listing_publication_state(
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        target_state=domain.ListingStatus.UNPUBLISHED,
        expected_version=previous_version,
    )
    assert updated is not None
    assert updated.status is domain.ListingStatus.UNPUBLISHED
    assert updated.version == previous_version + 1


async def test_set_listing_publication_state_rejects_stale_version(
    product_repository: ProductRepository, product_seed
) -> None:
    listing = product_seed.listing
    updated = await product_repository.set_listing_publication_state(
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        target_state=domain.ListingStatus.UNPUBLISHED,
        expected_version=listing.version + 1,
    )
    assert updated is None
