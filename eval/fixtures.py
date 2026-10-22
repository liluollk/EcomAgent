"""隔离 Eval Fixture(计划 05 Task 2 Step 3)。

:class:`IsolatedEnvironment` 为每个 case/repetition 创建完全独立的权威环境:

- 独立 Workspace、Actor(User + Membership + 权限/资源范围)与 Mock 平台
  业务状态(Store/Product/Listing 按 Scenario ``initial_state`` 种子);
- Session 由 Runtime 的 RunFactory 创建路径生成(每 Run 全新会话),天然
  不跨 case 共享;
- 能力版本(Agent/Skill/Tool/Model)按 stable_key 幂等落库复用,不随
  case 重复创建;
- 推进经与 API 宿主同源的 :class:`ProductApplication` 门面(组合核心
  ``build_product_application`` 位于 :mod:`composition.product_operations`,
  eval 不 import apps)。

不做 UI:种子直接经 runtime 持久层与 agents.product_operations 模型写入
(独立短事务提交,与测试的提交式种子同一模式);Backend 由调用方注入,
本模块不创建任何模型 Backend。

清理策略:teardown 清理(:meth:`IsolatedEnvironment.cleanup`,按外键逆序
删除本工作区行,不删全局能力行)。每 case 的 Workspace 本就独立,清理是为了
让失败用例的残留行不干扰后续断言(尤其 ``Run.status=QUEUED`` 的残留 Run 会
被下一次恢复扫描捡起);因此 :class:`eval.runner.EvalRunner` 默认在每 case
结束后清理(捕获先行,痕迹保留在 CaseResult 中)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.commerce.mock_adapter import MockCommerceAdapter
from agents.product_operations.domain.models import ListingStatus, StorePlatform
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    InventoryLevel,
    Listing,
    Product,
    Store,
)
from agents.product_operations.policy.models import PricePolicyConfig
from composition.product_operations import (
    ProductApplication,
    build_product_application,
)
from composition.provisioning.product_operations import (
    ModelConnectionProvisioningSpec,
    ProductOperationsCapabilities,
    ProductOperationsProvisioner,
)
from eval.capability_manifest import EvalCapabilityManifest, require_manifest_match
from eval.results import FinalState
from eval.schema import InventoryState, ListingState, Scenario
from runtime.backend.protocol import AgentBackend
from runtime.persistence.base import (
    AccountStatus,
    ExecutionMode,
    MembershipRole,
    ModelBackendType,
    ResourceScopeMode,
)
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)


EVAL_PRICE_CONFIG: Final = PricePolicyConfig(
    policy_version="eval-price-policy-v1",
    currency="CNY",
    min_allowed_price=Decimal("50.00"),
    max_price_change=Decimal("100.00"),
)
"""Eval 固定价格策略(协议的一部分:策略变更即协议漂移)。

与商品域闭环测试一致的边界:99 元对 128 元为合法变更。"""

EVAL_MODEL_SPEC: Final = ModelConnectionProvisioningSpec(
    stable_key="product-scripted",
    backend_type=ModelBackendType.OPENAI,
    base_url="https://scripted.model.invalid/v1",
    model="product-scripted",
    non_secret_options={},
)
"""Eval 发布的确定性脚本模型规范(每个隔离环境显式发布,单 Run 不重复)。"""

_DEFAULT_CURRENCY: Final = "CNY"
# 敏感字段哨兵值：真实种子到数据库，但不进入任何模型可见 Tool 输出。
_DEFAULT_COST_PRICE: Final = Decimal("37.42")

_ROLE_BY_NAME: Final[dict[str, MembershipRole]] = {
    "owner": MembershipRole.WORKSPACE_OWNER,
    "operator": MembershipRole.OPERATOR,
}

# 本工作区行清理(按外键依赖逆序);("SQL", 参数键) 二元组,键为 ws/user。
_CLEANUP_STATEMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("DELETE FROM audit_events WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM agent_events WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM operation_attempts WHERE workspace_id = :ws", "ws"),
    ("UPDATE tool_calls SET operation_id = NULL WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM confirmation_requests WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM input_requests WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM operations WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM tool_calls WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_attempts WHERE workspace_id = :ws", "ws"),
    ("UPDATE messages SET run_id = NULL WHERE workspace_id = :ws", "ws"),
    ("UPDATE runs SET trigger_message_id = NULL WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM messages WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_tool_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_skill_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_source_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_policy_snapshots WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM runs WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM sessions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.inventory_levels WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.price_locks WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.listings WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.fulfillment_locations WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.products WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.stores WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_permissions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_resource_scopes WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_resource_scope_modes WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM business_policy_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM business_policies WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM model_config_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM model_connections WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM workspace_memberships WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM workspaces WHERE id = :ws", "ws"),
    ("DELETE FROM users WHERE id = :user", "user"),
)


def _role_of(scenario: Scenario) -> MembershipRole:
    """Scenario Actor 角色名 → MembershipRole(未知名称按 OPERATOR 处理)。"""
    return _ROLE_BY_NAME.get(scenario.actor.role, MembershipRole.OPERATOR)


async def _seed_listing(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    symbol: str,
    sku: str,
    price: Decimal,
    currency: str | None,
    status: str | None,
    products_by_sku: dict[str, Product],
    stores_by_symbol: dict[str, Store],
) -> SeededListing:
    """按 Scenario 符号种子 Store/Product/Listing 并返回符号映射。

    Store 按 ``store_id`` 符号复用:一个店铺放多个 SKU 是常态,若每条 Listing
    都新建一个同名 Store,店铺引用解析会命中多个候选、被判成
    ``AMBIGUOUS_STORE_REFERENCE``,场景在工具调用阶段就崩掉。
    """
    store = stores_by_symbol.get(symbol)
    if store is None:
        store = Store(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            platform=StorePlatform.TAOBAO,
            external_store_ref=f"eval-{uuid.uuid4().hex[:12]}",
            name=symbol,
            aliases=[symbol],
        )
        stores_by_symbol[symbol] = store
        session.add(store)
        await session.flush()
    product = products_by_sku.get(sku)
    if product is None:
        product = Product(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            sku=sku,
            title=f"eval-product-{sku}",
        )
        products_by_sku[sku] = product
        session.add(product)
    await session.flush()
    listing = Listing(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        store_id=store.id,
        product_id=product.id,
        external_product_ref=f"eval-item-{uuid.uuid4().hex[:12]}",
        currency=currency if currency is not None else _DEFAULT_CURRENCY,
        current_price=price,
        cost_price=_DEFAULT_COST_PRICE,
        status=(
            ListingStatus(status.upper()) if status is not None else ListingStatus.PUBLISHED
        ),
    )
    session.add(listing)
    await session.flush()
    return SeededListing(symbol=symbol, store_id=store.id, listing_id=listing.id, sku=sku)


@dataclass(frozen=True)
class SeededListing:
    """一个已种子 Listing 的符号映射(Scenario 符号 ↔ 数据库标识)。"""

    symbol: str
    store_id: uuid.UUID
    listing_id: uuid.UUID
    sku: str


@dataclass(frozen=True)
class SeededInventory:
    """库存场景符号与持久化资源的对应关系。"""

    store_symbol: str
    sku: str
    location_ref: str
    inventory_level_id: uuid.UUID


@dataclass(frozen=True)
class IsolatedEnvironment:
    """单个 case/repetition 的独立权威环境(身份 + 业务状态 + 执行环境)。"""

    engine: AsyncEngine
    scenario: Scenario
    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    membership_id: uuid.UUID
    listings: tuple[SeededListing, ...]
    inventory_levels: tuple[SeededInventory, ...]
    application: ProductApplication
    commerce_write_observer: MockCommerceAdapter
    capabilities: ProductOperationsCapabilities
    capability_fingerprint: str | None = None

    @classmethod
    async def create(
        cls,
        engine: AsyncEngine,
        scenario: Scenario,
        *,
        backend: AgentBackend,
        manifest: EvalCapabilityManifest | None = None,
        execution_mode: ExecutionMode = ExecutionMode.CONFIRM_WRITES,
    ) -> IsolatedEnvironment:
        """种子身份与业务状态(独立短事务提交)并组装注入 Backend 的商品应用。"""
        workspace_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        membership_id = uuid.uuid4()
        seeded: list[SeededListing] = []
        seeded_inventory: list[SeededInventory] = []
        products_by_sku: dict[str, Product] = {}
        stores_by_symbol: dict[str, Store] = {}

        session = AsyncSession(engine, expire_on_commit=False)
        try:
            async with session.begin():
                workspace = Workspace(
                    id=workspace_id,
                    name=f"{scenario.workspace.name}-{uuid.uuid4().hex[:8]}",
                    status=AccountStatus.ACTIVE,
                    execution_mode=execution_mode,
                )
                user = User(
                    id=actor_id,
                    login=f"eval-{uuid.uuid4().hex[:12]}",
                    password_hash="eval-no-login",
                    status=AccountStatus.ACTIVE,
                )
                membership = WorkspaceMembership(
                    id=membership_id,
                    workspace_id=workspace_id,
                    user_id=actor_id,
                    role=_role_of(scenario),
                    status=AccountStatus.ACTIVE,
                )
                # 无 relationship() 声明的复合外键(membership_permissions 等)
                # 不保证跨表插入顺序;按依赖分批 flush,被引用行先落库。
                session.add_all([workspace, user, membership])
                await session.flush()
                for spec in scenario.initial_state.listings:
                    if spec.price is None:
                        # initial_state 中 price 为 None 表示该字段"不提供":
                        # 不种子该 Listing(权威状态确属缺失)。
                        continue
                    seeded.append(
                        await _seed_listing(
                            session,
                            workspace_id=workspace_id,
                            symbol=spec.store_id,
                            sku=spec.sku,
                            price=spec.price,
                            currency=spec.currency,
                            status=spec.status,
                            products_by_sku=products_by_sku,
                            stores_by_symbol=stores_by_symbol,
                        )
                    )
                for inventory_spec in scenario.initial_state.inventory_levels:
                    listing = next(
                        (
                            item
                            for item in seeded
                            if item.symbol == inventory_spec.store_id
                            and item.sku == inventory_spec.sku
                        ),
                        None,
                    )
                    product = products_by_sku.get(inventory_spec.sku)
                    if listing is None or product is None:
                        raise ValueError(
                            "inventory seed requires a matching listing for "
                            f"{inventory_spec.store_id}/{inventory_spec.sku}"
                        )
                    location = FulfillmentLocation(
                        id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        store_id=listing.store_id,
                        external_location_ref=inventory_spec.location_ref,
                        name=inventory_spec.location_ref,
                        aliases=[inventory_spec.location_ref],
                    )
                    session.add(location)
                    await session.flush()
                    level = InventoryLevel(
                        id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        location_id=location.id,
                        product_id=product.id,
                        available_quantity=inventory_spec.available_quantity,
                    )
                    session.add(level)
                    seeded_inventory.append(
                        SeededInventory(
                            store_symbol=inventory_spec.store_id,
                            sku=inventory_spec.sku,
                            location_ref=inventory_spec.location_ref,
                            inventory_level_id=level.id,
                        )
                    )
                session.add_all(
                    MembershipPermission(
                        workspace_id=workspace_id,
                        membership_id=membership_id,
                        permission=permission,
                    )
                    for permission in scenario.actor.permissions
                )
                if scenario.actor.store_scope_mode != "NONE":
                    persistence_mode = ResourceScopeMode(
                        scenario.actor.store_scope_mode
                    )
                    session.add(
                        MembershipResourceScopeMode(
                            workspace_id=workspace_id,
                            membership_id=membership_id,
                            resource_type="store",
                            mode=persistence_mode,
                        )
                    )
                    if persistence_mode is ResourceScopeMode.SELECTED:
                        requested_symbols = set(scenario.actor.store_ids)
                        available_symbols = {listing.symbol for listing in seeded}
                        missing_symbols = requested_symbols - available_symbols
                        if missing_symbols:
                            raise ValueError(
                                "actor store scope references unseeded stores: "
                                + ", ".join(sorted(missing_symbols))
                            )
                        scoped_store_ids = {
                            listing.store_id
                            for listing in seeded
                            if listing.symbol in requested_symbols
                        }
                        session.add_all(
                            MembershipResourceScope(
                                workspace_id=workspace_id,
                                membership_id=membership_id,
                                resource_type="store",
                                resource_id=str(store_id),
                            )
                            for store_id in sorted(scoped_store_ids)
                        )
        finally:
            await session.close()

        # 幂等发布能力版本(全局按 stable_key 复用);装配只消费已发布快照。
        capabilities = await ProductOperationsProvisioner(
            lambda: AsyncSession(engine, expire_on_commit=False)
        ).provision(
            workspace_id=workspace_id,
            model=EVAL_MODEL_SPEC,
            pricing_policy=EVAL_PRICE_CONFIG,
        )
        if manifest is not None:
            await require_manifest_match(
                lambda: AsyncSession(engine, expire_on_commit=False),
                capabilities,
                manifest,
            )
        application = await build_product_application(
            engine,
            capabilities=capabilities,
            backend=backend,
            workspace_id=workspace_id,
            actor_id=actor_id,
            membership_id=membership_id,
            executor_id=f"eval-executor-{uuid.uuid4().hex[:8]}",
        )
        if not isinstance(application.adapter, MockCommerceAdapter):
            raise TypeError("Eval fixtures require the deterministic MockCommerceAdapter")
        for fault in scenario.faults:
            application.adapter.faults.inject(fault)
        return cls(
            engine=engine,
            scenario=scenario,
            workspace_id=workspace_id,
            actor_id=actor_id,
            membership_id=membership_id,
            listings=tuple(seeded),
            inventory_levels=tuple(seeded_inventory),
            application=application,
            commerce_write_observer=application.adapter,
            capabilities=capabilities,
            capability_fingerprint=(
                manifest.capability_fingerprint if manifest is not None else None
            ),
        )

    def store_id_for_sku(self, sku: str) -> uuid.UUID | None:
        """按 SKU 反查种子店铺(供缺店铺 Input Request 的确定性回答)。"""
        for seeded in self.listings:
            if seeded.sku == sku:
                return seeded.store_id
        return None

    def store_id_for_symbol(self, symbol: str, *, sku: str | None = None) -> uuid.UUID | None:
        """按场景符号键解析用户明确选择的店铺,可用 SKU 消除重复店铺歧义。"""
        matches = [
            listing
            for listing in self.listings
            if listing.symbol == symbol and (sku is None or listing.sku == sku)
        ]
        if not matches:
            return None
        distinct_ids = {listing.store_id for listing in matches}
        return next(iter(distinct_ids)) if len(distinct_ids) == 1 else None

    def store_symbol_for_id(self, store_id: str) -> str | None:
        """内部店铺 UUID → 场景符号键，供 Eval Trace 与 Gold 断言比较。"""
        matches = {
            listing.symbol
            for listing in self.listings
            if str(listing.store_id) == store_id
        }
        return next(iter(matches)) if len(matches) == 1 else None

    @property
    def single_store_id(self) -> uuid.UUID | None:
        """唯一种子店铺(单店铺场景的回退回答)。"""
        return self.listings[0].store_id if self.listings else None

    async def capture_final_state(self, *, logical_writes: int) -> FinalState:
        """读取平台侧权威最终状态(符号键),供 CaseResult 与 Scorer 比较。"""
        session = AsyncSession(self.engine, expire_on_commit=False)
        try:
            states: list[ListingState] = []
            for seeded in self.listings:
                row = (
                    await session.scalars(
                        select(Listing).where(
                            Listing.workspace_id == self.workspace_id,
                            Listing.id == seeded.listing_id,
                        )
                    )
                ).one_or_none()
                if row is None:
                    continue
                states.append(
                    ListingState(
                        store_id=seeded.symbol,
                        sku=seeded.sku,
                        price=row.current_price,
                        currency=row.currency,
                        status=row.status.value,
                    )
                )
            inventory_states: list[InventoryState] = []
            for seeded_level in self.inventory_levels:
                level = await session.get(
                    InventoryLevel, seeded_level.inventory_level_id
                )
                if level is None:
                    continue
                inventory_states.append(
                    InventoryState(
                        store_id=seeded_level.store_symbol,
                        sku=seeded_level.sku,
                        location_ref=seeded_level.location_ref,
                        available_quantity=level.available_quantity,
                    )
                )
            return FinalState(
                listings=tuple(states),
                inventory_levels=tuple(inventory_states),
                logical_writes=logical_writes,
            )
        finally:
            await session.close()

    async def aclose(self) -> None:
        """关闭商品应用的 Adapter Session(环境生命周期终点)。"""
        await self.application.aclose()

    async def cleanup(self) -> None:
        """Teardown:按外键逆序删除本工作区行,并删除本次引用的全局能力行。

        全局能力行(Agent/Skill/Tool 版本,按 stable_key 幂等落库)随本次
        组装引用一并删除——与 tests/agents/product_operations 的提交式夹具
        同一约定:残留的 ``agents.stable_key`` 等全局行会与后续按同稳定键
        插入的测试种子冲突。
        """
        session = AsyncSession(self.engine, expire_on_commit=False)
        try:
            async with session.begin():
                for text, key in _CLEANUP_STATEMENTS:
                    params: dict[str, Any] = (
                        {"user": self.actor_id} if key == "user" else {"ws": self.workspace_id}
                    )
                    await session.execute(sa.text(text), params)
                capability = dict(self.application.capability_ids)
                for definition_id in capability.get(
                    "tool_definition_ids", {}
                ).values():
                    await session.execute(
                        sa.text("DELETE FROM tool_versions WHERE tool_id = :id"),
                        {"id": definition_id},
                    )
                    await session.execute(
                        sa.text("DELETE FROM tool_definitions WHERE id = :id"),
                        {"id": definition_id},
                    )
                for id_field, table in (
                    ("skill_version_id", "skill_versions"),
                    ("skill_id", "skills"),
                    ("agent_version_id", "agent_versions"),
                    ("agent_id", "agents"),
                ):
                    row_id = capability.get(id_field)
                    if row_id is not None:
                        await session.execute(
                            sa.text(f"DELETE FROM {table} WHERE id = :id"),
                            {"id": row_id},
                        )
        finally:
            await session.close()


__all__ = [
    "EVAL_MODEL_SPEC",
    "EVAL_PRICE_CONFIG",
    "IsolatedEnvironment",
    "SeededInventory",
    "SeededListing",
]
