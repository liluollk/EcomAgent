"""Workspace 工作台 overview 聚合接口测试(工作台收口设计 §5、§8、§9.1)。

覆盖:Owner/Operator 角色与真实执行模式、双 Workspace 隔离、SELECTED/ALL
店铺范围、无 Agent 绑定返回 null、多绑定返回确定性 409、非成员 403。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.persistence.models import Listing, Product, Store
from apps.api.main import create_app
from runtime.access.tokens import issue_token
from runtime.persistence.base import (
    AccountStatus,
    ConfigVersionStatus,
    ExecutionMode,
    MembershipRole,
)
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    WorkspaceAgentBinding,
)
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)


@dataclass(frozen=True)
class SeededStore:
    store: Store
    product: Product
    listing: Listing


@dataclass(frozen=True)
class SeededWorkspace:
    """一个 Workspace + 可选店铺/Agent 绑定;双 Workspace 测试创建两份。"""

    workspace: Workspace
    owner: User
    owner_membership: WorkspaceMembership
    operator: User | None = None
    operator_membership: WorkspaceMembership | None = None
    outsider: User | None = None
    stores: tuple[SeededStore, ...] = ()
    agent: Agent | None = None
    agent_versions: tuple[AgentVersion, ...] = ()
    binding: WorkspaceAgentBinding | None = None

    @property
    def id(self) -> UUID:
        return self.workspace.id

    @property
    def owner_token_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(self.owner.id)}"}

    @property
    def operator_token_headers(self) -> dict[str, str] | None:
        if self.operator is None:
            return None
        return {"Authorization": f"Bearer {issue_token(self.operator.id)}"}


async def _cleanup(session: AsyncSession, seeded: list[SeededWorkspace]) -> None:
    user_ids: set[UUID] = set()
    for item in seeded:
        workspace_id = item.id
        user_ids.add(item.owner.id)
        if item.operator is not None:
            user_ids.add(item.operator.id)
        if item.outsider is not None:
            user_ids.add(item.outsider.id)
        await session.execute(
            sa.delete(WorkspaceAgentBinding).where(
                WorkspaceAgentBinding.workspace_id == workspace_id
            )
        )
        if item.agent_versions:
            await session.execute(
                sa.delete(AgentVersion).where(
                    AgentVersion.id.in_([version.id for version in item.agent_versions])
                )
            )
        if item.agent is not None:
            await session.execute(sa.delete(Agent).where(Agent.id == item.agent.id))
        for seeded_store in item.stores:
            await session.execute(
                sa.delete(Listing).where(Listing.id == seeded_store.listing.id)
            )
            await session.execute(
                sa.delete(Product).where(Product.id == seeded_store.product.id)
            )
            await session.execute(
                sa.delete(Store).where(Store.id == seeded_store.store.id)
            )
        await session.execute(
            sa.delete(MembershipPermission).where(
                MembershipPermission.workspace_id == workspace_id
            )
        )
        await session.execute(
            sa.delete(MembershipResourceScope).where(
                MembershipResourceScope.workspace_id == workspace_id
            )
        )
        await session.execute(
            sa.delete(MembershipResourceScopeMode).where(
                MembershipResourceScopeMode.workspace_id == workspace_id
            )
        )
        await session.execute(
            sa.delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id
            )
        )
        await session.execute(sa.delete(Workspace).where(Workspace.id == workspace_id))
    if user_ids:
        await session.execute(sa.delete(User).where(User.id.in_(user_ids)))
    await session.commit()


async def _seed_store(
    session: AsyncSession, workspace_id: UUID, name: str, price: str
) -> SeededStore:
    store = Store(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"ovr-{uuid.uuid4().hex[:12]}",
        name=name,
    )
    product = Product(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        sku=f"SKU-OVR-{uuid.uuid4().hex[:6]}",
        title=f"{name} 商品",
    )
    listing = Listing(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        store_id=store.id,
        product_id=product.id,
        external_product_ref=f"ext-{uuid.uuid4().hex[:12]}",
        currency="CNY",
        current_price=Decimal(price),
        cost_price=Decimal("0.01"),
        version=1,
    )
    # 跨 schema 外键未在 ORM 声明:先 flush 店铺/商品,再插入 Listing。
    session.add_all([store, product])
    await session.flush()
    session.add(listing)
    await session.flush()
    return SeededStore(store=store, product=product, listing=listing)


async def _seed_workspace(
    session: AsyncSession,
    *,
    name: str,
    execution_mode: ExecutionMode = ExecutionMode.CONFIRM_WRITES,
    with_operator: bool = False,
    with_outsider: bool = False,
    stores: tuple[tuple[str, str], ...] = (),
    with_agent: bool = False,
    enabled_bindings: int = 1,
    scope_mode: str = "ALL",
    scope_store_ids: tuple[UUID, ...] = (),
) -> SeededWorkspace:
    owner = User(
        id=uuid.uuid4(),
        login=f"ovr-owner-{uuid.uuid4().hex[:10]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    workspace = Workspace(
        id=uuid.uuid4(), name=name, execution_mode=execution_mode
    )
    owner_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=owner.id,
        role=MembershipRole.WORKSPACE_OWNER,
        status=AccountStatus.ACTIVE,
    )
    session.add_all([owner, workspace, owner_membership])
    await session.flush()

    operator: User | None = None
    operator_membership: WorkspaceMembership | None = None
    if with_operator:
        operator = User(
            id=uuid.uuid4(),
            login=f"ovr-op-{uuid.uuid4().hex[:10]}",
            password_hash="not-a-real-hash",
            status=AccountStatus.ACTIVE,
        )
        operator_membership = WorkspaceMembership(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=operator.id,
            role=MembershipRole.OPERATOR,
            status=AccountStatus.ACTIVE,
        )
        session.add_all([operator, operator_membership])
        await session.flush()

    outsider: User | None = None
    if with_outsider:
        outsider = User(
            id=uuid.uuid4(),
            login=f"ovr-out-{uuid.uuid4().hex[:10]}",
            password_hash="not-a-real-hash",
            status=AccountStatus.ACTIVE,
        )
        session.add(outsider)
        await session.flush()

    # Owner 资源范围:ALL / SELECTED(指定店铺)/ NONE(调用方稍后自行补充)。
    if scope_mode == "ALL":
        session.add(
            MembershipResourceScopeMode(
                workspace_id=workspace.id,
                membership_id=owner_membership.id,
                resource_type="store",
                mode="ALL",
            )
        )
    elif scope_mode == "SELECTED":
        session.add(
            MembershipResourceScopeMode(
                workspace_id=workspace.id,
                membership_id=owner_membership.id,
                resource_type="store",
                mode="SELECTED",
            )
        )
        for store_id in scope_store_ids:
            session.add(
                MembershipResourceScope(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    membership_id=owner_membership.id,
                    resource_type="store",
                    resource_id=str(store_id),
                )
            )
    session.add(
        MembershipPermission(
            workspace_id=workspace.id,
            membership_id=owner_membership.id,
            permission="product.read",
        )
    )
    await session.flush()

    seeded_stores_list = []
    for store_name, price in stores:
        seeded_stores_list.append(
            await _seed_store(session, workspace.id, store_name, price)
        )
    seeded_stores = tuple(seeded_stores_list)

    agent: Agent | None = None
    agent_versions: list[AgentVersion] = []
    binding: WorkspaceAgentBinding | None = None
    if with_agent:
        agent = Agent(
            id=uuid.uuid4(),
            stable_key=f"agent-ovr-{uuid.uuid4().hex[:8]}",
            name="商品运营 Agent",
        )
        session.add(agent)
        await session.flush()
        agent_version = AgentVersion(
            id=uuid.uuid4(),
            agent_id=agent.id,
            version=1,
            status=ConfigVersionStatus.PUBLISHED,
            definition_json={"tools": [], "skills": [], "sources": []},
            definition_hash="ovr-test-hash",
        )
        session.add(agent_version)
        agent_versions.append(agent_version)
        await session.flush()
        for index in range(enabled_bindings):
            if index > 0:
                # 唯一约束 (workspace_id, agent_version_id):多绑定需多个版本。
                agent_version = AgentVersion(
                    id=uuid.uuid4(),
                    agent_id=agent.id,
                    version=1 + index,
                    status=ConfigVersionStatus.PUBLISHED,
                    definition_json={"tools": [], "skills": [], "sources": []},
                    definition_hash=f"ovr-test-hash-{index}",
                )
                session.add(agent_version)
                agent_versions.append(agent_version)
                await session.flush()
            binding = WorkspaceAgentBinding(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                agent_version_id=agent_version.id,
                enabled=True,
            )
            session.add(binding)
        await session.flush()

    return SeededWorkspace(
        workspace=workspace,
        owner=owner,
        owner_membership=owner_membership,
        operator=operator,
        operator_membership=operator_membership,
        outsider=outsider,
        stores=seeded_stores,
        agent=agent,
        agent_versions=tuple(agent_versions),
        binding=binding,
    )


@pytest.fixture()
async def overview_env(
    engine: AsyncEngine,
) -> AsyncIterator[tuple[AsyncClient, list[SeededWorkspace]]]:
    """默认环境:工作区 A(Owner+Operator+两店铺+启用绑定)、工作区 B(一店铺)、非成员。"""
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: list[SeededWorkspace] = []
    try:
        ws_a = await _seed_workspace(
            session,
            name="品牌 A",
            with_operator=True,
            with_outsider=True,
            stores=(("淘宝旗舰店", "128.00"), ("抖音小店", "118.00")),
            with_agent=True,
        )
        ws_b = await _seed_workspace(
            session,
            name="品牌 B",
            stores=(("品牌 B 专属店", "99.00"),),
            with_agent=True,
        )
        # 工作区 B 复用工作区 A 的 Owner(同一用户,成员身份独立)。
        membership_b = WorkspaceMembership(
            id=uuid.uuid4(),
            workspace_id=ws_b.id,
            user_id=ws_a.owner.id,
            role=MembershipRole.WORKSPACE_OWNER,
            status=AccountStatus.ACTIVE,
        )
        scope_all = MembershipResourceScopeMode(
            workspace_id=ws_b.id,
            membership_id=membership_b.id,
            resource_type="store",
            mode="ALL",
        )
        permission_b = MembershipPermission(
            workspace_id=ws_b.id,
            membership_id=membership_b.id,
            permission="product.read",
        )
        session.add(membership_b)
        await session.flush()
        session.add_all([scope_all, permission_b])
        await session.commit()
        seeded = [ws_a, ws_b]

        app = create_app(engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=ws_a.owner_token_headers,
        ) as client:
            yield client, seeded
    finally:
        await session.rollback()
        await _cleanup(session, seeded)
        await session.close()


async def test_owner_overview_returns_owner_role_and_real_execution_mode(
    overview_env: tuple[AsyncClient, list[SeededWorkspace]],
) -> None:
    client, seeded = overview_env
    ws_a = seeded[0]
    response = await client.get(f"/api/workspaces/{ws_a.id}/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace"]["name"] == "品牌 A"
    assert body["workspace"]["execution_mode"] == "CONFIRM_WRITES"
    assert body["workspace"]["state_version"] >= 0
    assert body["current_membership"]["role"] == "WORKSPACE_OWNER"
    assert "product.read" in body["current_membership"]["permissions"]


async def test_operator_overview_returns_operator_role(
    engine: AsyncEngine, overview_env: tuple[AsyncClient, list[SeededWorkspace]]
) -> None:
    _client, seeded = overview_env
    ws_a = seeded[0]
    operator_headers = ws_a.operator_token_headers
    assert operator_headers is not None
    app = create_app(engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=operator_headers,
    ) as operator_client:
        response = await operator_client.get(f"/api/workspaces/{ws_a.id}/overview")
    assert response.status_code == 200
    assert response.json()["current_membership"]["role"] == "OPERATOR"


async def test_two_workspaces_do_not_leak_stores_or_agent(
    overview_env: tuple[AsyncClient, list[SeededWorkspace]],
) -> None:
    client, seeded = overview_env
    ws_a, ws_b = seeded
    first = (await client.get(f"/api/workspaces/{ws_a.id}/overview")).json()
    second = (await client.get(f"/api/workspaces/{ws_b.id}/overview")).json()
    assert {store["name"] for store in first["stores"]} == {"淘宝旗舰店", "抖音小店"}
    assert {store["name"] for store in second["stores"]} == {"品牌 B 专属店"}
    assert first["agent"]["stable_key"] != second["agent"]["stable_key"]


async def test_selected_store_scope_returns_only_authorized_stores(
    engine: AsyncEngine,
) -> None:
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: list[SeededWorkspace] = []
    try:
        seeded_ws = await _seed_workspace(
            session,
            name="范围受限",
            stores=(("授权店", "100.00"), ("未授权店", "90.00")),
            with_agent=True,
            scope_mode="NONE",
        )
        # SELECTED 范围:仅授权第一个店铺。
        session.add(
            MembershipResourceScopeMode(
                workspace_id=seeded_ws.id,
                membership_id=seeded_ws.owner_membership.id,
                resource_type="store",
                mode="SELECTED",
            )
        )
        session.add(
            MembershipResourceScope(
                workspace_id=seeded_ws.id,
                membership_id=seeded_ws.owner_membership.id,
                resource_type="store",
                    resource_id=str(seeded_ws.stores[0].store.id),
            )
        )
        await session.commit()
        seeded = [seeded_ws]
        app = create_app(engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=seeded_ws.owner_token_headers,
        ) as client:
            response = await client.get(f"/api/workspaces/{seeded_ws.id}/overview")
        assert response.status_code == 200
        assert [store["name"] for store in response.json()["stores"]] == ["授权店"]
    finally:
        await session.rollback()
        await _cleanup(session, seeded)
        await session.close()


async def test_overview_without_agent_binding_returns_null_agent(
    engine: AsyncEngine,
) -> None:
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: list[SeededWorkspace] = []
    try:
        seeded_ws = await _seed_workspace(session, name="无绑定工作区")
        await session.commit()
        seeded = [seeded_ws]
        app = create_app(engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=seeded_ws.owner_token_headers,
        ) as client:
            response = await client.get(f"/api/workspaces/{seeded_ws.id}/overview")
        assert response.status_code == 200
        assert response.json()["agent"] is None
    finally:
        await session.rollback()
        await _cleanup(session, seeded)
        await session.close()


async def test_overview_ignores_agent_binding_that_is_not_effective_yet(
    engine: AsyncEngine,
) -> None:
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: list[SeededWorkspace] = []
    try:
        seeded_ws = await _seed_workspace(
            session, name="未来绑定工作区", with_agent=True
        )
        assert seeded_ws.binding is not None
        seeded_ws.binding.effective_at = datetime.now(UTC) + timedelta(days=1)
        await session.commit()
        seeded = [seeded_ws]
        app = create_app(engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=seeded_ws.owner_token_headers,
        ) as client:
            response = await client.get(f"/api/workspaces/{seeded_ws.id}/overview")
        assert response.status_code == 200
        assert response.json()["agent"] is None
    finally:
        await session.rollback()
        await _cleanup(session, seeded)
        await session.close()


async def test_multiple_enabled_agent_bindings_return_409(
    engine: AsyncEngine,
) -> None:
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: list[SeededWorkspace] = []
    try:
        seeded_ws = await _seed_workspace(
            session, name="多绑定工作区", with_agent=True, enabled_bindings=2
        )
        await session.commit()
        seeded = [seeded_ws]
        app = create_app(engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers=seeded_ws.owner_token_headers,
        ) as client:
            response = await client.get(f"/api/workspaces/{seeded_ws.id}/overview")
        assert response.status_code == 409
        assert response.json()["code"] == "WORKSPACE_AGENT_CONFIGURATION_INVALID"
    finally:
        await session.rollback()
        await _cleanup(session, seeded)
        await session.close()


async def test_non_member_overview_returns_403(
    engine: AsyncEngine,
) -> None:
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: list[SeededWorkspace] = []
    try:
        seeded_ws = await _seed_workspace(session, name="私有工作区", with_outsider=True)
        await session.commit()
        seeded = [seeded_ws]
        assert seeded_ws.outsider is not None
        app = create_app(engine=engine)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {issue_token(seeded_ws.outsider.id)}"},
        ) as client:
            response = await client.get(f"/api/workspaces/{seeded_ws.id}/overview")
        assert response.status_code == 403
        assert response.json()["code"] == "PERMISSION_DENIED"
    finally:
        await session.rollback()
        await _cleanup(session, seeded)
        await session.close()
