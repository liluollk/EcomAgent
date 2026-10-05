"""隔离 Eval Fixture(计划 05 Task 2 Step 3)。

:class:`IsolatedEnvironment` 为每个 case/repetition 创建完全独立的权威环境:

- 独立 Workspace、Actor(User + Membership + 权限/资源范围)与 Mock 平台
  业务状态(Store/Product/Listing 按 Scenario ``initial_state`` 种子);
- Session 由 Runtime 的 RunFactory 创建路径生成(每 Run 全新会话),天然
  不跨 case 共享;
- 能力版本(Agent/Skill/Tool/Model)按 stable_key 幂等落库复用,不随
  case 重复创建;
- 推进经与 apps/worker 同源的 :class:`ProductRuntime` 门面(组合核心
  ``build_product_runtime`` 已下沉至 :mod:`adapters.commerce.product_runtime`,
  eval 不 import apps)。

不做 UI:种子直接经 runtime 持久层与 agents.product_operations 模型写入
(独立短事务提交,与测试的提交式种子同一模式);Backend 由调用方注入,
本模块不创建任何模型 Backend。

清理策略:teardown 清理(:meth:`IsolatedEnvironment.cleanup`,按外键逆序
删除本工作区行,不删全局能力行)。独立 Workspace 本可无需删除,但失败的
推进会把 ADVANCE_RUN Job 释放回 PENDING,而 ``LeaseService.claim`` 领取
不区分 workspace——残留 Job 会被后续 case 的 Worker 推进误领取;因此
:class:`eval.runner.EvalRunner` 默认在每 case 结束后清理(捕获先行,
痕迹保留在 CaseResult 中)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.commerce.product_runtime import (
    ProductRuntime,
    bootstrap_product_capabilities,
    build_product_runtime,
)
from agents.product_operations.domain.models import ListingStatus, StorePlatform
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.policy.models import PricePolicyConfig
from eval.results import FinalState
from eval.schema import ListingState, Scenario
from runtime.backend.protocol import AgentBackend
from runtime.persistence.base import (
    AccountStatus,
    ExecutionMode,
    MembershipRole,
    ResourceScopeMode,
)
from runtime.persistence.models.identity import (
    MembershipPermission,
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

_DEFAULT_CURRENCY: Final = "CNY"
_DEFAULT_COST_PRICE: Final = Decimal("0.00")

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
    ("DELETE FROM run_jobs WHERE workspace_id = :ws", "ws"),
    ("UPDATE messages SET run_id = NULL WHERE workspace_id = :ws", "ws"),
    ("UPDATE runs SET trigger_message_id = NULL WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM messages WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_tool_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_skill_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_source_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_policy_snapshots WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM runs WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM sessions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.price_locks WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.listings WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.products WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.stores WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_permissions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_resource_scopes WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_resource_scope_modes WHERE workspace_id = :ws", "ws"),
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
) -> SeededListing:
    """按 Scenario 符号种子 Store/Product/Listing 并返回符号映射。"""
    store = Store(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"eval-{uuid.uuid4().hex[:12]}",
        name=symbol,
        aliases=[symbol],
    )
    product = Product(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        sku=sku,
        title=f"eval-product-{sku}",
    )
    session.add_all([store, product])
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
            ListingStatus(status.upper()) if status is not None else ListingStatus.ACTIVE
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
class IsolatedEnvironment:
    """单个 case/repetition 的独立权威环境(身份 + 业务状态 + 运行面)。"""

    engine: AsyncEngine
    scenario: Scenario
    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    membership_id: uuid.UUID
    listings: tuple[SeededListing, ...]
    runtime: ProductRuntime

    @classmethod
    async def create(
        cls, engine: AsyncEngine, scenario: Scenario, *, backend: AgentBackend
    ) -> IsolatedEnvironment:
        """种子身份与业务状态(独立短事务提交)并组装注入 Backend 的运行面。"""
        workspace_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        membership_id = uuid.uuid4()
        seeded: list[SeededListing] = []

        session = AsyncSession(engine, expire_on_commit=False)
        try:
            async with session.begin():
                workspace = Workspace(
                    id=workspace_id,
                    name=f"{scenario.workspace.name}-{uuid.uuid4().hex[:8]}",
                    status=AccountStatus.ACTIVE,
                    execution_mode=ExecutionMode.CONFIRM_WRITES,
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
                session.add_all(
                    [
                        MembershipPermission(
                            workspace_id=workspace_id,
                            membership_id=membership_id,
                            permission="product.read",
                        ),
                        MembershipPermission(
                            workspace_id=workspace_id,
                            membership_id=membership_id,
                            permission="product.price.write",
                        ),
                        MembershipResourceScopeMode(
                            workspace_id=workspace_id,
                            membership_id=membership_id,
                            resource_type="store",
                            mode=ResourceScopeMode.ALL,
                        ),
                    ]
                )
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
                        )
                    )
        finally:
            await session.close()

        # 能力版本幂等落库(全局按 stable_key 复用),再组装注入 Backend 的运行面。
        await bootstrap_product_capabilities(engine, workspace_id)
        runtime = await build_product_runtime(
            engine,
            backend=backend,
            workspace_id=workspace_id,
            actor_id=actor_id,
            membership_id=membership_id,
            price_config=EVAL_PRICE_CONFIG,
            worker_id=f"eval-worker-{uuid.uuid4().hex[:8]}",
        )
        return cls(
            engine=engine,
            scenario=scenario,
            workspace_id=workspace_id,
            actor_id=actor_id,
            membership_id=membership_id,
            listings=tuple(seeded),
            runtime=runtime,
        )

    def store_id_for_sku(self, sku: str) -> uuid.UUID | None:
        """按 SKU 反查种子店铺(供缺店铺 Input Request 的确定性回答)。"""
        for seeded in self.listings:
            if seeded.sku == sku:
                return seeded.store_id
        return None

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
            return FinalState(listings=tuple(states), logical_writes=logical_writes)
        finally:
            await session.close()

    async def aclose(self) -> None:
        """关闭运行面的 Adapter Session(环境生命周期终点)。"""
        await self.runtime.aclose()

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
                capability = dict(self.runtime.capability_ids)
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
    "EVAL_PRICE_CONFIG",
    "IsolatedEnvironment",
    "SeededListing",
]
