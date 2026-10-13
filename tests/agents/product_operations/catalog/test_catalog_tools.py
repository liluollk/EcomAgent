"""Catalog 能力测试(P1 Task 5;设计 §4.2、§4.1)。

覆盖:
- ``search_products`` 输入 Schema 只暴露 ``query`` / ``store_refs``,不含内部 ID;
- 只返回授权店铺内的 PUBLISHED Listing,输出不含成本价、平台凭证与内部 UUID;
- 店铺引用按名称/别名解析;越权引用显式拒绝,未知引用 ``NOT_FOUND``;
- 缺少 ``product.read`` 权限拒绝;无授权店铺返回空结果。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.catalog.models import SearchProductsInput
from agents.product_operations.catalog.service import CatalogService
from agents.product_operations.catalog.tools import SEARCH_PRODUCTS_DEFINITION
from agents.product_operations.domain.models import ListingStatus, StorePlatform
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.shared.errors import (
    NOT_FOUND,
    ProductOperationError,
)
from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext
from runtime.access.authorization import PERMISSION_DENIED, RESOURCE_SCOPE_DENIED
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


@dataclass(frozen=True)
class _Seed:
    """一个 Workspace 内的确定性商品域种子(淘宝店授权、豆音店不授权)。"""

    workspace: Workspace
    taobao: Store
    douyin: Store


async def _seed(session: AsyncSession) -> _Seed:
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
    cup = Product(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        sku="SKU-001",
        title="轻量保温杯 500ml",
    )
    mug = Product(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        sku="SKU-002",
        title="陶瓷马克杯",
    )
    session.add(workspace)
    await session.flush()
    session.add_all((taobao, douyin, cup, mug))
    await session.flush()
    session.add_all(
        (
            Listing(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                store_id=taobao.id,
                product_id=cup.id,
                external_product_ref="item-1",
                currency="CNY",
                current_price=Decimal("128.00"),
                cost_price=Decimal("88.00"),
            ),
            Listing(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                store_id=douyin.id,
                product_id=cup.id,
                external_product_ref="item-2",
                currency="CNY",
                current_price=Decimal("118.00"),
                cost_price=Decimal("80.00"),
            ),
            Listing(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                store_id=taobao.id,
                product_id=mug.id,
                external_product_ref="item-3",
                currency="CNY",
                current_price=Decimal("45.50"),
                cost_price=Decimal("30.00"),
            ),
        )
    )
    await session.flush()
    return _Seed(workspace=workspace, taobao=taobao, douyin=douyin)


def _actor(
    *,
    workspace_id: uuid.UUID,
    store_ids: frozenset[str],
    permissions: frozenset[str] = frozenset({"product.read"}),
) -> ActorContext:
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=permissions,
        resource_scopes={
            "store": ResourceScope(mode="SELECTED", resource_ids=store_ids),
        },
        request_id="req-catalog",
    )


@dataclass(frozen=True)
class _Env:
    seed: _Seed
    service: CatalogService
    operator: ActorContext


@pytest.fixture()
async def env(db_session: AsyncSession) -> _Env:
    seed = await _seed(db_session)
    return _Env(
        seed=seed,
        service=CatalogService(db_session),
        operator=_actor(
            workspace_id=seed.workspace.id, store_ids=frozenset({str(seed.taobao.id)})
        ),
    )


def test_search_products_schema_exposes_refs_not_internal_ids() -> None:
    assert set(SEARCH_PRODUCTS_DEFINITION.input_json_schema["properties"]) == {
        "query",
        "store_refs",
    }
    # 输入模型本身也不接受内部标识字段。
    assert set(SearchProductsInput.model_fields) == {"query", "store_refs"}


async def test_search_products_returns_only_authorized_stores(env: _Env) -> None:
    result = await env.service.search_products(actor=env.operator)
    assert {item.sku for item in result.items} == {"SKU-001", "SKU-002"}
    for item in result.items:
        assert {ref.store_ref for ref in item.stores} == {"淘宝旗舰店"}


async def test_search_products_filters_by_query(env: _Env) -> None:
    result = await env.service.search_products(actor=env.operator, query="保温杯")
    assert [item.sku for item in result.items] == ["SKU-001"]


async def test_search_products_resolves_store_ref_and_alias(env: _Env) -> None:
    by_name = await env.service.search_products(actor=env.operator, store_refs=("淘宝旗舰店",))
    by_alias = await env.service.search_products(actor=env.operator, store_refs=("旗舰店",))
    assert {item.sku for item in by_name.items} == {"SKU-001", "SKU-002"}
    assert {item.sku for item in by_alias.items} == {"SKU-001", "SKU-002"}


async def test_search_products_does_not_leak_internal_values(env: _Env) -> None:
    result = await env.service.search_products(actor=env.operator)
    dumped = json.dumps(result.model_dump(mode="json"))
    # 成本价不输出,内部 UUID 不作为模型可见字段出现。
    assert "88.00" not in dumped
    assert str(env.seed.taobao.id) not in dumped
    assert "store_id" not in dumped


async def test_search_products_rejects_out_of_scope_store_ref(env: _Env) -> None:
    with pytest.raises(ToolAccessError) as error:
        await env.service.search_products(actor=env.operator, store_refs=("豆音小店",))
    assert error.value.reason_code == RESOURCE_SCOPE_DENIED


async def test_search_products_unknown_store_ref_is_not_found(env: _Env) -> None:
    with pytest.raises(ProductOperationError) as error:
        await env.service.search_products(actor=env.operator, store_refs=("不存在的店铺",))
    assert error.value.code == NOT_FOUND


async def test_search_products_requires_read_permission(env: _Env) -> None:
    actor = _actor(
        workspace_id=env.seed.workspace.id,
        store_ids=frozenset({str(env.seed.taobao.id)}),
        permissions=frozenset(),
    )
    with pytest.raises(ToolAccessError) as error:
        await env.service.search_products(actor=actor)
    assert error.value.reason_code == PERMISSION_DENIED


async def test_search_products_without_authorized_stores_returns_empty(env: _Env) -> None:
    # 无 ``store`` scope 映射即"不可见任何店铺"(access.py 语义)。
    actor = ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=env.seed.workspace.id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=frozenset({"product.read"}),
        resource_scopes={},
        request_id="req-catalog-no-store",
    )
    result = await env.service.search_products(actor=actor)
    assert result.items == ()


async def test_search_products_excludes_unpublished_listings(
    env: _Env, db_session: AsyncSession
) -> None:
    listing = await db_session.scalar(
        select(Listing)
        .join(Product, Listing.product_id == Product.id)
        .where(
            Listing.workspace_id == env.seed.workspace.id,
            Listing.store_id == env.seed.taobao.id,
            Product.sku == "SKU-001",
        )
    )
    assert listing is not None
    listing.status = ListingStatus.UNPUBLISHED
    await db_session.flush()

    result = await env.service.search_products(actor=env.operator)
    assert "SKU-001" not in {item.sku for item in result.items}
