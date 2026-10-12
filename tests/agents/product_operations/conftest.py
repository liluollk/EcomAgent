"""商品域测试夹具(计划 03 Task 1)。

复用根 conftest(tests/conftest.py)的 ``engine`` fixture:测试库已由
Alembic ``upgrade head`` 准备。此处提供商品域 Session(回滚隔离)、种子器
与被测 Repository。
"""

import sys
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)
from runtime.persistence.base import (
    ExecutionMode,
    MembershipRole,
    ModelBackendType,
    ResourceScopeMode,
)
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScopeMode,
    User,
    WorkspaceMembership,
)


# tests/fixtures 不是 pytest 收集路径,显式加入 sys.path 以导入共享夹具。
TESTS_DIR = Path(__file__).resolve().parents[2]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fixtures.product_operations import (
    ProductOperationsSeed,
    ProductOperationsSeeder,
)

from agents.product_operations.persistence.models import Listing
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.policy.models import PricePolicyConfig
from apps.worker.assembly import (
    ProductApplication,
    build_product_application,
)
from composition.provisioning.product_operations import (
    ModelConnectionProvisioningSpec,
    ProductOperationsProvisioner,
)


_PRODUCT_MODEL_SPEC: Final = ModelConnectionProvisioningSpec(
    stable_key="product-scripted",
    backend_type=ModelBackendType.OPENAI,
    base_url="https://scripted.model.invalid/v1",
    model="product-scripted",
    non_secret_options={},
)
"""测试夹具显式发布的确定性脚本模型规范(装配只消费已发布快照)。"""

PRODUCT_PRICE_POLICY: Final = PricePolicyConfig(
    policy_version="price-policy-v1",
    currency="CNY",
    min_allowed_price=Decimal("50.00"),
    max_price_change=Decimal("100.00"),
)
"""夹具显式发布的价格规则:装配期只能从已发布行读到它,不再由调用方直接注入。"""


@pytest.fixture()
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个测试一个独立 Session;结束时不提交,回滚以保证测试间隔离。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


@pytest.fixture()
def product_seeder(db_session: AsyncSession) -> ProductOperationsSeeder:
    return ProductOperationsSeeder(db_session)


@pytest.fixture()
async def product_seed(product_seeder: ProductOperationsSeeder) -> ProductOperationsSeed:
    """独立 Workspace + Store/Product/Listing/PriceLock 种子。"""
    return await product_seeder.seed()


@pytest.fixture()
def product_repository(db_session: AsyncSession) -> ProductRepository:
    return ProductRepository(db_session)


@pytest.fixture()
def listing(product_seed: ProductOperationsSeed) -> Listing:
    """默认种子的 Listing 行(version/updated_at 等 server default 已载入)。"""
    return product_seed.listing


# ---------------------------------------------------------------------------
# 端到端改价闭环夹具(计划 03 Task 6;参照 tests/runtime/integration 的
# crash_harness 模式:种子行提交 + 独立 Session 工厂 + 结束清理)。
# ---------------------------------------------------------------------------


class ScriptedAgentBackend:
    """按轮次回放事件脚本:每次 stream 消耗一个脚本,耗尽后返回空完成。"""

    def __init__(self, rounds: Sequence[Sequence[BackendEvent]] = ()) -> None:
        self.rounds: list[list[BackendEvent]] = [list(round) for round in rounds]
        self.requests: list[BackendRequest] = []

    def set_rounds(self, rounds: Sequence[Sequence[BackendEvent]]) -> None:
        self.rounds = [list(round) for round in rounds]

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        self.requests.append(request)
        events = (
            self.rounds.pop(0) if self.rounds else [BackendCompleted(finish_reason="stop")]
        )
        for event in events:
            if cancellation.is_cancelled:
                return
            yield event


@dataclass(frozen=True)
class ProductApplicationIds:
    """product_application 夹具的已提交种子标识(身份 + 商品域 + 组装能力)。

    组装能力标识由发布快照经 ``replace`` 补全;
    清理时跳过仍为 ``None`` 的项。
    """

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    membership_id: uuid.UUID
    store_id: uuid.UUID
    product_id: uuid.UUID
    listing_id: uuid.UUID
    agent_id: uuid.UUID | None = None
    agent_version_id: uuid.UUID | None = None
    skill_id: uuid.UUID | None = None
    skill_version_id: uuid.UUID | None = None
    tool_definition_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    model_config_version_id: uuid.UUID | None = None


async def _seed_committed_environment(
    engine: AsyncEngine,
) -> ProductApplicationIds:
    """提交身份(含 product.price.write 权限与 store ALL 范围)、淘宝旗舰店
    SKU-001(现价 128.00,无活动锁价)种子行,并返回全部标识。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            seeder = ProductOperationsSeeder(session)
            seed = await seeder.seed()
            # 该商品业务夹具覆盖确认/恢复流程，显式使用确认模式；Eval Runner
            # 则单独显式使用 AUTO_EXECUTE。
            seed.workspace.execution_mode = ExecutionMode.CONFIRM_WRITES
            # 对齐计划原文用例:店铺名/别名为"淘宝旗舰店",SKU 为 SKU-001;
            # 移除默认活动锁价(锁价分支由 price policy 单测覆盖)。
            seed.store.name = "淘宝旗舰店"
            seed.store.aliases = ["淘宝旗舰店"]
            seed.product.sku = "SKU-001"
            await session.delete(seed.price_lock)
            await session.flush()

            workspace_id = seed.workspace.id
            user_id = uuid.uuid4()
            membership_id = uuid.uuid4()
            session.add(User(id=user_id, login=f"user-{uuid.uuid4()}", password_hash="t"))
            session.add(
                WorkspaceMembership(
                    id=membership_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role=MembershipRole.OPERATOR,
                )
            )
            await session.flush()
            session.add(
                MembershipPermission(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    permission="product.read",
                )
            )
            session.add(
                MembershipPermission(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    permission="product.price.write",
                )
            )
            session.add(
                MembershipResourceScopeMode(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    resource_type="store",
                    mode=ResourceScopeMode.ALL,
                )
            )
            await session.flush()
            return ProductApplicationIds(
                workspace_id=workspace_id,
                user_id=user_id,
                membership_id=membership_id,
                store_id=seed.store.id,
                product_id=seed.product.id,
                listing_id=seed.listing.id,
            )
    finally:
        await session.close()


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
    ("DELETE FROM messages WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_tool_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_skill_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_source_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM run_policy_snapshots WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM runs WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM sessions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM model_config_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM model_connections WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.inventory_levels WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.price_locks WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.listings WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.fulfillment_locations WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.products WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM product_operations.stores WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_permissions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_resource_scopes WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM membership_resource_scope_modes WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM workspace_memberships WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM business_policy_versions WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM business_policies WHERE workspace_id = :ws", "ws"),
    ("DELETE FROM users WHERE id = :user", "user"),
    ("DELETE FROM workspaces WHERE id = :ws", "ws"),
)


async def _cleanup_committed_environment(
    engine: AsyncEngine, ids: ProductApplicationIds
) -> None:
    """按外键依赖倒序删除已提交种子行,并移除组装写入的全局能力行。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            for text, key in _CLEANUP_STATEMENTS:
                params: dict[str, Any] = (
                    {"user": ids.user_id} if key == "user" else {"ws": ids.workspace_id}
                )
                await session.execute(sa.text(text), params)
            for definition_id in ids.tool_definition_ids.values():
                await session.execute(
                    sa.text("DELETE FROM tool_versions WHERE tool_id = :id"),
                    {"id": definition_id},
                )
                await session.execute(
                    sa.text("DELETE FROM tool_definitions WHERE id = :id"),
                    {"id": definition_id},
                )
            if ids.skill_version_id is not None:
                await session.execute(
                    sa.text("DELETE FROM skill_versions WHERE id = :id"),
                    {"id": ids.skill_version_id},
                )
            if ids.skill_id is not None:
                await session.execute(
                    sa.text("DELETE FROM skills WHERE id = :id"), {"id": ids.skill_id}
                )
            if ids.agent_version_id is not None:
                await session.execute(
                    sa.text("DELETE FROM agent_versions WHERE id = :id"),
                    {"id": ids.agent_version_id},
                )
            if ids.agent_id is not None:
                await session.execute(
                    sa.text("DELETE FROM agents WHERE id = :id"), {"id": ids.agent_id}
                )
    finally:
        await session.close()


@pytest.fixture()
def commerce_adapter_for_application():
    """可由单个测试模块覆盖的商品平台 Adapter 注入点。"""
    return


@pytest.fixture()
async def product_application(
    engine: AsyncEngine, commerce_adapter_for_application
) -> AsyncIterator[ProductApplication]:
    """组装完整 Runtime 的商品改价闭环夹具(真库 + 确定性脚本模型)。"""
    ids = await _seed_committed_environment(engine)
    backend = ScriptedAgentBackend()
    capabilities = await ProductOperationsProvisioner(
        lambda: AsyncSession(engine, expire_on_commit=False)
    ).provision(
        workspace_id=ids.workspace_id,
        model=_PRODUCT_MODEL_SPEC,
        pricing_policy=PRODUCT_PRICE_POLICY,
    )
    application = await build_product_application(
        engine,
        capabilities=capabilities,
        backend=backend,
        workspace_id=ids.workspace_id,
        actor_id=ids.user_id,
        membership_id=ids.membership_id,
        worker_id=f"product-worker-{uuid.uuid4()}",
        commerce_adapter=commerce_adapter_for_application,
        adapter_key=(
            "rest-commerce"
            if commerce_adapter_for_application is not None
            else "mock-commerce"
        ),
    )
    capability = application.capability_ids
    ids = replace(
        ids,
        agent_id=capability["agent_id"],
        agent_version_id=capability["agent_version_id"],
        skill_id=capability["skill_id"],
        skill_version_id=capability["skill_version_id"],
        tool_definition_ids=dict(capability["tool_definition_ids"]),
        model_config_version_id=capability["model_config_version_id"],
    )
    application.scripted_backend = backend
    application.seed_ids = ids
    try:
        yield application
    finally:
        await application.aclose()
        await _cleanup_committed_environment(engine, ids)


@pytest.fixture()
def rebuild_product_application(
    engine: AsyncEngine, product_application, commerce_adapter_for_application
):
    """按同一组合能力重建 ProductApplication(模拟进程重启后的新实例)。

    复用 ``product_application`` 已提交的种子标识;价格规则来自同一份已发布策略
    (重新发布是幂等的),调用方负责 ``aclose`` 新建实例。
    """
    ids = product_application.seed_ids

    async def _rebuild() -> ProductApplication:
        capabilities = await ProductOperationsProvisioner(
            lambda: AsyncSession(engine, expire_on_commit=False)
        ).provision(
            workspace_id=ids.workspace_id,
            model=_PRODUCT_MODEL_SPEC,
            pricing_policy=PRODUCT_PRICE_POLICY,
        )
        return await build_product_application(
            engine,
            capabilities=capabilities,
            backend=ScriptedAgentBackend(),
            workspace_id=ids.workspace_id,
            actor_id=ids.user_id,
            membership_id=ids.membership_id,
            worker_id=f"product-worker-{uuid.uuid4()}",
            commerce_adapter=commerce_adapter_for_application,
            adapter_key=(
                "rest-commerce"
                if commerce_adapter_for_application is not None
                else "mock-commerce"
            ),
        )

    return _rebuild
