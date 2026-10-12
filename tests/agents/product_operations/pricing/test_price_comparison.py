"""Pricing 价格比较测试(P1 Task 6;设计 §4.3)。

覆盖:
- 确定性价格行、最低价、最高价与价差;
- 只比较**已发布** Listing;多币种直接 ``CURRENCY_MISMATCH`` 拒绝;
- 店铺引用按名称/别名解析;无授权店铺或无可比较行时显式失败;
- 缺少 ``product.read`` 权限拒绝。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import (
    ListingStatus,
    StorePlatform,
)
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.pricing.models import (
    CURRENCY_MISMATCH,
    NO_PUBLISHED_LISTING,
)
from agents.product_operations.pricing.service import PricingService
from agents.product_operations.shared.errors import ProductOperationError
from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext
from runtime.access.authorization import PERMISSION_DENIED
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


@dataclass(frozen=True)
class _Seed:
    workspace: Workspace
    taobao: Store
    douyin: Store
    taobao_listing: Listing
    douyin_listing: Listing


async def _seed(
    session: AsyncSession, *, douyin_currency: str = "CNY"
) -> _Seed:
    """两店铺同一 SKU 的已发布 Listing:淘宝 90.00、豆音 110.00。"""
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    taobao = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"taobao-{uuid.uuid4()}",
        name="淘宝旗舰店",
        aliases=["旗舰店"],
    )
    douyin = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.DOUYIN,
        external_store_ref=f"douyin-{uuid.uuid4()}",
        name="豆音小店",
        aliases=[],
    )
    product = Product(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        sku="SKU-1",
        title="轻量保温杯 500ml",
    )
    session.add(workspace)
    await session.flush()
    session.add_all((taobao, douyin, product))
    await session.flush()
    taobao_listing = Listing(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=taobao.id,
        product_id=product.id,
        external_product_ref="item-taobao",
        currency="CNY",
        current_price=Decimal("90.00"),
        cost_price=Decimal("60.00"),
        status=ListingStatus.PUBLISHED,
    )
    douyin_listing = Listing(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=douyin.id,
        product_id=product.id,
        external_product_ref="item-douyin",
        currency=douyin_currency,
        current_price=Decimal("110.00"),
        cost_price=Decimal("70.00"),
        status=ListingStatus.PUBLISHED,
    )
    session.add_all((taobao_listing, douyin_listing))
    await session.flush()
    return _Seed(
        workspace=workspace,
        taobao=taobao,
        douyin=douyin,
        taobao_listing=taobao_listing,
        douyin_listing=douyin_listing,
    )


def _actor(
    *,
    workspace_id: uuid.UUID,
    store_ids: frozenset[str] | None,
    permissions: frozenset[str] = frozenset({"product.read"}),
) -> ActorContext:
    scopes = (
        {}
        if store_ids is None
        else {"store": ResourceScope(mode="SELECTED", resource_ids=store_ids)}
    )
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=permissions,
        resource_scopes=scopes,
        request_id="req-pricing",
    )


@dataclass(frozen=True)
class _Env:
    seed: _Seed
    service: PricingService
    operator: ActorContext


@pytest.fixture()
async def env(db_session: AsyncSession) -> _Env:
    seed = await _seed(db_session)
    return _Env(
        seed=seed,
        service=PricingService(db_session),
        operator=_actor(
            workspace_id=seed.workspace.id,
            store_ids=frozenset({str(seed.taobao.id), str(seed.douyin.id)}),
        ),
    )


async def test_compare_listing_prices_returns_deterministic_summary(
    env: _Env,
) -> None:
    result = await env.service.compare_listing_prices(
        actor=env.operator, sku="SKU-1", store_refs=("淘宝旗舰店", "豆音小店")
    )
    assert result.lowest_price == Decimal("90.00")
    assert result.highest_price == Decimal("110.00")
    assert result.price_spread == Decimal("20.00")
    assert result.currency == "CNY"
    assert [(row.store_ref, row.price) for row in result.rows] == [
        ("淘宝旗舰店", Decimal("90.00")),
        ("豆音小店", Decimal("110.00")),
    ]


async def test_compare_listing_prices_resolves_alias(env: _Env) -> None:
    result = await env.service.compare_listing_prices(
        actor=env.operator, sku="SKU-1", store_refs=("旗舰店",)
    )
    assert [row.store_ref for row in result.rows] == ["淘宝旗舰店"]
    assert result.price_spread == Decimal("0.00")


async def test_compare_listing_prices_rejects_multiple_currencies(
    db_session: AsyncSession,
) -> None:
    seed = await _seed(db_session, douyin_currency="USD")
    service = PricingService(db_session)
    actor = _actor(
        workspace_id=seed.workspace.id,
        store_ids=frozenset({str(seed.taobao.id), str(seed.douyin.id)}),
    )
    with pytest.raises(ProductOperationError) as exc:
        await service.compare_listing_prices(
            actor=actor, sku="SKU-1", store_refs=("淘宝旗舰店", "豆音小店")
        )
    assert exc.value.code == CURRENCY_MISMATCH


async def test_compare_listing_prices_skips_unpublished(
    env: _Env, db_session: AsyncSession
) -> None:
    env.seed.douyin_listing.status = ListingStatus.UNPUBLISHED
    await db_session.flush()
    result = await env.service.compare_listing_prices(
        actor=env.operator, sku="SKU-1"
    )
    assert [row.store_ref for row in result.rows] == ["淘宝旗舰店"]


async def test_compare_listing_prices_without_authorized_stores_fails(
    env: _Env,
) -> None:
    actor = _actor(workspace_id=env.seed.workspace.id, store_ids=None)
    with pytest.raises(ProductOperationError) as exc:
        await env.service.compare_listing_prices(actor=actor, sku="SKU-1")
    assert exc.value.code == NO_PUBLISHED_LISTING


async def test_compare_listing_prices_requires_read_permission(env: _Env) -> None:
    actor = _actor(
        workspace_id=env.seed.workspace.id,
        store_ids=frozenset({str(env.seed.taobao.id)}),
        permissions=frozenset(),
    )
    with pytest.raises(ToolAccessError) as exc:
        await env.service.compare_listing_prices(actor=actor, sku="SKU-1")
    assert exc.value.reason_code == PERMISSION_DENIED


async def test_compare_listing_prices_unknown_sku_fails(env: _Env) -> None:
    with pytest.raises(ProductOperationError) as exc:
        await env.service.compare_listing_prices(
            actor=env.operator, sku="SKU-missing"
        )
    assert exc.value.code == NO_PUBLISHED_LISTING
