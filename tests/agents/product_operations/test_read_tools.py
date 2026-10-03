"""只读商品 Tool 测试(计划 03 Task 2)。

覆盖:
- compare_listing_prices 只返回授权店铺(计划原文用例);
- search_products 按 SKU/标题模糊查询且只命中授权店铺内商品;
- get_product_snapshot 返回 Output Schema 白名单字段,不泄漏 cost_price
  等内部字段;
- 未授权店铺的商品与 Listing 不出现在任何结果;
- 缺少 product.read 权限、越权 store 一律显式拒绝;
- 查询始终携带 workspace_id,跨 Workspace 不可见;
- ToolDefinition:effect=READ、required_permissions、schema_hash 与
  Schema 一致;ToolBinding 连接 handler。
"""

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.tools import ReadTools
from agents.product_operations.tools.compare_listing_prices import (
    COMPARE_LISTING_PRICES_BINDING,
    compare_listing_prices,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.errors import ProductNotFoundError, ToolAccessError
from agents.product_operations.tools.get_product_snapshot import (
    GET_PRODUCT_SNAPSHOT_BINDING,
    get_product_snapshot,
)
from agents.product_operations.tools.search_products import (
    SEARCH_PRODUCTS_BINDING,
    search_products,
)
from runtime.access.actor import ActorContext
from runtime.access.authorization import PERMISSION_DENIED, RESOURCE_SCOPE_DENIED
from runtime.capabilities.validation import compute_schema_hash
from runtime.interfaces.identifiers import ResourceScope
from runtime.interfaces.tools import ToolEffect
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


SNAPSHOT_FIELDS: Final = frozenset(
    {
        "store_id",
        "product_id",
        "listing_id",
        "sku",
        "title",
        "current_price",
        "currency",
        "status",
        "listing_version",
        "redacted_fields",
    }
)


@dataclass(frozen=True)
class Catalog:
    """一个 Workspace 内的确定性商品域种子(淘宝店授权、豆音店不授权)。"""

    workspace: Workspace
    taobao: Store
    douyin: Store
    products: dict[str, Product]
    listings: dict[tuple[str, uuid.UUID], Listing]


async def _seed_catalog(session: AsyncSession) -> Catalog:
    """创建独立 Workspace:淘宝/豆音两店、三个商品与四条 Listing。"""
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    taobao = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"taobao-{uuid.uuid4()}",
        name="淘宝旗舰店",
        aliases=[],
    )
    douyin = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.DOUYIN,
        external_store_ref=f"douyin-{uuid.uuid4()}",
        name="豆音小店",
        aliases=[],
    )
    products = {
        "SKU-001": Product(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            sku="SKU-001",
            title="轻量保温杯 500ml",
        ),
        "SKU-002": Product(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            sku="SKU-002",
            title="陶瓷马克杯",
        ),
        "SKU-003": Product(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            sku="SKU-003",
            title="便携咖啡壶",
        ),
    }

    def listing(store: Store, sku: str, price: str, cost: str) -> Listing:
        return Listing(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            store_id=store.id,
            product_id=products[sku].id,
            external_product_ref=f"item-{uuid.uuid4()}",
            currency="CNY",
            current_price=Decimal(price),
            cost_price=Decimal(cost),
        )

    listings = {
        ("SKU-001", taobao.id): listing(taobao, "SKU-001", "128.00", "88.00"),
        ("SKU-001", douyin.id): listing(douyin, "SKU-001", "118.00", "80.00"),
        ("SKU-002", taobao.id): listing(taobao, "SKU-002", "45.50", "30.00"),
        ("SKU-003", douyin.id): listing(douyin, "SKU-003", "199.00", "120.00"),
    }
    session.add(workspace)
    await session.flush()
    session.add_all((taobao, douyin, *products.values()))
    await session.flush()
    session.add_all(listings.values())
    await session.flush()
    return Catalog(
        workspace=workspace,
        taobao=taobao,
        douyin=douyin,
        products=products,
        listings=listings,
    )


def _actor(
    *,
    workspace_id: uuid.UUID,
    store_ids: frozenset[str],
    permissions: frozenset[str] = frozenset({"product.read"}),
) -> ActorContext:
    """构造测试用 ActorContext:store scope 与 resource_id = str(store.id)。"""
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=permissions,
        resource_scopes={
            "store": ResourceScope(mode="SELECTED", resource_ids=store_ids),
        },
        request_id="req-read-tools",
    )


@pytest.fixture()
async def catalog(db_session: AsyncSession) -> Catalog:
    return await _seed_catalog(db_session)


@pytest.fixture()
def read_tools(db_session: AsyncSession) -> ReadTools:
    return ReadTools(session=db_session)


@pytest.fixture()
def operator(catalog: Catalog) -> ActorContext:
    """只授权淘宝店的运营者;豆音店存在但不授权。"""
    return _actor(
        workspace_id=catalog.workspace.id,
        store_ids=frozenset({str(catalog.taobao.id)}),
    )


# ---- compare_listing_prices(计划原文用例 + 白名单) ----


async def test_compare_prices_returns_only_authorized_stores(
    read_tools: ReadTools, operator: ActorContext, catalog: Catalog
) -> None:
    result = await read_tools.compare_listing_prices(actor=operator, sku="SKU-001")
    assert {row.store_id for row in result.items} == {str(catalog.taobao.id)}
    # 豆音店同 SKU 的 118.00 不可见,行内是淘宝店自己的价格与状态。
    assert len(result.items) == 1
    row = result.items[0]
    assert row.current_price == Decimal("128.00")
    assert row.currency == "CNY"
    assert row.status == "ACTIVE"
    assert row.listing_version == 1


async def test_compare_listing_prices_returns_whitelisted_row_fields(
    read_tools: ReadTools, operator: ActorContext
) -> None:
    result = await read_tools.compare_listing_prices(actor=operator, sku="SKU-001")
    assert set(result.model_dump()) == set(
        COMPARE_LISTING_PRICES_DEFINITION.output_json_schema["properties"]
    )
    assert set(result.model_dump()["items"][0]) == {
        "store_id",
        "current_price",
        "currency",
        "status",
        "listing_version",
    }
    dumped = json.dumps(result.model_dump(mode="json"))
    assert "cost_price" not in dumped


async def test_compare_listing_prices_returns_empty_items_for_unknown_sku(
    read_tools: ReadTools, operator: ActorContext
) -> None:
    result = await read_tools.compare_listing_prices(actor=operator, sku="SKU-404")
    assert result.items == ()


# ---- search_products ----


async def test_search_products_matches_sku_or_title_within_authorized_stores(
    read_tools: ReadTools, operator: ActorContext
) -> None:
    by_title = await read_tools.search_products(actor=operator, query="保温杯")
    assert [item.sku for item in by_title.items] == ["SKU-001"]

    by_sku_prefix = await read_tools.search_products(actor=operator, query="SKU-00")
    # SKU-003 只在豆音店上架,不在授权范围内,不出现。
    assert [item.sku for item in by_sku_prefix.items] == ["SKU-001", "SKU-002"]

    douyin_only = await read_tools.search_products(actor=operator, query="咖啡壶")
    assert douyin_only.items == ()


async def test_search_results_hide_unauthorized_store_listings(
    read_tools: ReadTools, operator: ActorContext, catalog: Catalog
) -> None:
    result = await read_tools.search_products(actor=operator, query="SKU-001")
    assert len(result.items) == 1
    item = result.items[0]
    assert set(item.model_dump()) == {"product_id", "sku", "title", "stores"}
    assert set(item.stores[0].model_dump()) == {"store_id", "platform"}
    assert {ref.store_id for ref in item.stores} == {str(catalog.taobao.id)}
    assert set(result.model_dump()) == set(
        SEARCH_PRODUCTS_DEFINITION.output_json_schema["properties"]
    )
    dumped = json.dumps(result.model_dump(mode="json"))
    assert str(catalog.douyin.id) not in dumped
    assert "cost_price" not in dumped


# ---- get_product_snapshot ----


async def test_get_product_snapshot_returns_whitelisted_fields(
    read_tools: ReadTools, operator: ActorContext, catalog: Catalog
) -> None:
    snapshot = await read_tools.get_product_snapshot(
        actor=operator,
        store_id=str(catalog.taobao.id),
        sku="SKU-001",
    )
    assert set(snapshot.model_dump()) == SNAPSHOT_FIELDS
    assert set(snapshot.model_dump()) == set(
        GET_PRODUCT_SNAPSHOT_DEFINITION.output_json_schema["properties"]
    )
    assert snapshot.store_id == str(catalog.taobao.id)
    assert snapshot.product_id == str(catalog.products["SKU-001"].id)
    assert snapshot.listing_id == str(catalog.listings[("SKU-001", catalog.taobao.id)].id)
    assert snapshot.sku == "SKU-001"
    assert snapshot.title == "轻量保温杯 500ml"
    assert snapshot.current_price == Decimal("128.00")
    assert snapshot.currency == "CNY"
    assert snapshot.status == "ACTIVE"
    assert snapshot.listing_version == 1
    # cost_price 数值不输出,只以脱敏摘要出现(摘要本身引用字段名是预期行为)。
    assert snapshot.redacted_fields == ("cost_price",)
    dumped = json.dumps(snapshot.model_dump(mode="json"))
    assert "88.00" not in dumped


async def test_get_product_snapshot_denies_unauthorized_store(
    read_tools: ReadTools, operator: ActorContext, catalog: Catalog
) -> None:
    with pytest.raises(ToolAccessError) as exc_info:
        await read_tools.get_product_snapshot(
            actor=operator,
            store_id=str(catalog.douyin.id),
            sku="SKU-001",
        )
    assert exc_info.value.reason_code == RESOURCE_SCOPE_DENIED


async def test_get_product_snapshot_rejects_unknown_sku(
    read_tools: ReadTools, operator: ActorContext, catalog: Catalog
) -> None:
    with pytest.raises(ProductNotFoundError):
        await read_tools.get_product_snapshot(
            actor=operator,
            store_id=str(catalog.taobao.id),
            sku="SKU-404",
        )


# ---- 拒绝语义 ----


async def test_read_tools_reject_missing_product_read_permission(
    read_tools: ReadTools, catalog: Catalog
) -> None:
    actor = _actor(
        workspace_id=catalog.workspace.id,
        store_ids=frozenset({str(catalog.taobao.id)}),
        permissions=frozenset(),
    )
    calls = (
        lambda: read_tools.search_products(actor=actor, query="SKU-00"),
        lambda: read_tools.get_product_snapshot(
            actor=actor, store_id=str(catalog.taobao.id), sku="SKU-001"
        ),
        lambda: read_tools.compare_listing_prices(actor=actor, sku="SKU-001"),
    )
    for call in calls:
        with pytest.raises(ToolAccessError) as exc_info:
            await call()
        assert exc_info.value.reason_code == PERMISSION_DENIED


async def test_requested_stores_outside_scope_are_denied(
    read_tools: ReadTools, operator: ActorContext, catalog: Catalog
) -> None:
    douyin_id = str(catalog.douyin.id)
    with pytest.raises(ToolAccessError) as exc_info:
        await read_tools.compare_listing_prices(
            actor=operator, sku="SKU-001", store_ids=(douyin_id,)
        )
    assert exc_info.value.reason_code == RESOURCE_SCOPE_DENIED

    with pytest.raises(ToolAccessError) as exc_info:
        await read_tools.search_products(
            actor=operator, query="SKU-00", store_ids=(douyin_id,)
        )
    assert exc_info.value.reason_code == RESOURCE_SCOPE_DENIED


# ---- Workspace 隔离 ----


async def test_queries_are_scoped_to_actor_workspace(
    read_tools: ReadTools,
    db_session: AsyncSession,
    catalog: Catalog,
    operator: ActorContext,
) -> None:
    other = await _seed_catalog(db_session)
    other_actor = _actor(
        workspace_id=other.workspace.id,
        store_ids=frozenset({str(other.taobao.id)}),
    )

    search = await read_tools.search_products(actor=operator, query="SKU-001")
    assert {item.product_id for item in search.items} == {
        str(catalog.products["SKU-001"].id)
    }

    compare = await read_tools.compare_listing_prices(actor=operator, sku="SKU-001")
    assert {row.store_id for row in compare.items} == {str(catalog.taobao.id)}

    other_compare = await read_tools.compare_listing_prices(
        actor=other_actor, sku="SKU-001"
    )
    assert {row.store_id for row in other_compare.items} == {str(other.taobao.id)}

    other_snapshot = await read_tools.get_product_snapshot(
        actor=other_actor, store_id=str(other.taobao.id), sku="SKU-001"
    )
    assert other_snapshot.product_id == str(other.products["SKU-001"].id)

    with pytest.raises(ToolAccessError) as exc_info:
        await read_tools.get_product_snapshot(
            actor=operator, store_id=str(other.taobao.id), sku="SKU-001"
        )
    assert exc_info.value.reason_code == RESOURCE_SCOPE_DENIED


# ---- 定义与绑定 ----


@pytest.mark.parametrize(
    "definition",
    [
        SEARCH_PRODUCTS_DEFINITION,
        GET_PRODUCT_SNAPSHOT_DEFINITION,
        COMPARE_LISTING_PRICES_DEFINITION,
    ],
    ids=["search_products", "get_product_snapshot", "compare_listing_prices"],
)
def test_definitions_declare_read_effect_and_consistent_schema_hash(
    definition: object,
) -> None:
    assert definition.effect is ToolEffect.READ  # type: ignore[attr-defined]
    assert definition.required_permissions == frozenset({"product.read"})  # type: ignore[attr-defined]
    assert definition.source_kind == "LOCAL"  # type: ignore[attr-defined]
    assert definition.schema_hash == compute_schema_hash(  # type: ignore[attr-defined]
        definition.input_json_schema,  # type: ignore[attr-defined]
        definition.output_json_schema,  # type: ignore[attr-defined]
    )


def test_tool_bindings_connect_definitions_to_handlers() -> None:
    assert SEARCH_PRODUCTS_BINDING.definition is SEARCH_PRODUCTS_DEFINITION
    assert SEARCH_PRODUCTS_BINDING.handler is search_products
    assert GET_PRODUCT_SNAPSHOT_BINDING.definition is GET_PRODUCT_SNAPSHOT_DEFINITION
    assert GET_PRODUCT_SNAPSHOT_BINDING.handler is get_product_snapshot
    assert (
        COMPARE_LISTING_PRICES_BINDING.definition is COMPARE_LISTING_PRICES_DEFINITION
    )
    assert COMPARE_LISTING_PRICES_BINDING.handler is compare_listing_prices
    for binding in (
        SEARCH_PRODUCTS_BINDING,
        GET_PRODUCT_SNAPSHOT_BINDING,
        COMPARE_LISTING_PRICES_BINDING,
    ):
        assert binding.write_adapter_capabilities == frozenset()
