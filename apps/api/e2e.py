"""E2E 种子端点(阶段 4 Gate;仅在 ``RUNTIME_E2E=1`` 时注册)。

为 Playwright/手工端到端验证提供幂等种子(无外部真实服务依赖):

- 固定账号 ``e2e-operator`` / ``e2e-password``(WORKSPACE_OWNER,
  授予 ``product.read``/``product.price.write`` 与 store ALL 范围);
- 工作区 ``品牌一``,店铺 ``淘宝旗舰店``(别名同名),商品 ``SKU-001``,
  Listing 初始价 128.00 CNY;
- 能力版本(Agent/Skill/Tool/Model Connection)复用
  :func:`apps.worker.assembly._bootstrap_capabilities`(幂等,与 Worker
  组合根共用同一份稳定 Key);
- 重复调用返回同一套标识,并把 Listing 价格复位到 128.00、清空该工作区
  上一次验证遗留的运行面数据(sessions/runs/messages/events/requests/
  tool_calls/operations),保证每次 E2E 从确定状态开始。

本端点只服务本地/CI 端到端验证,不在生产装配中注册。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.persistence.models import Listing, Product, Store
from apps.api.dependencies import SessionDep
from apps.worker.assembly import _bootstrap_capabilities
from runtime.access.passwords import hash_password
from runtime.persistence.base import (
    AccountStatus,
    ExecutionMode,
    MembershipRole,
    ResourceScopeMode,
)
from runtime.persistence.models.configuration import WorkspaceAgentBinding
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)


router = APIRouter()


E2E_LOGIN: Final = "e2e-operator"
E2E_PASSWORD: Final = "e2e-password"
E2E_WORKSPACE_NAME: Final = "品牌一"
E2E_STORE_NAME: Final = "淘宝旗舰店"
E2E_SKU: Final = "SKU-001"
E2E_INITIAL_PRICE: Final = Decimal("128.00")
E2E_COST_PRICE: Final = Decimal("88.00")
E2E_CURRENCY: Final = "CNY"


class E2ESeedView(BaseModel):
    """POST /api/e2e/seed 响应:E2E 全流程所需的标识。"""

    workspace_id: UUID
    workspace_name: str
    agent_id: UUID
    store_id: UUID
    store_name: str
    sku: str
    listing_id: UUID
    initial_price: str
    login: str
    password: str


@router.post("/api/e2e/seed", response_model=E2ESeedView)
async def seed_e2e_environment(request: Request, session: SessionDep) -> E2ESeedView:
    """幂等创建/复位 E2E 环境(见模块 docstring);返回全部标识。"""
    engine: AsyncEngine = request.app.state.engine
    user = (
        await session.scalars(select(User).where(User.login == E2E_LOGIN))
    ).one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(),
            login=E2E_LOGIN,
            password_hash=hash_password(E2E_PASSWORD),
            status=AccountStatus.ACTIVE,
        )
        session.add(user)
        await session.flush()

    membership_row = (
        await session.execute(
            select(WorkspaceMembership, Workspace)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                WorkspaceMembership.user_id == user.id,
                Workspace.name == E2E_WORKSPACE_NAME,
            )
        )
    ).first()
    if membership_row is None:
        workspace = Workspace(
            id=uuid.uuid4(),
            name=E2E_WORKSPACE_NAME,
            status=AccountStatus.ACTIVE,
            execution_mode=ExecutionMode.CONFIRM_WRITES,
        )
        session.add(workspace)
        await session.flush()
        membership = WorkspaceMembership(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role=MembershipRole.WORKSPACE_OWNER,
            status=AccountStatus.ACTIVE,
        )
        session.add(membership)
        await session.flush()
        session.add_all(
            [
                MembershipPermission(
                    workspace_id=workspace.id,
                    membership_id=membership.id,
                    permission="product.read",
                ),
                MembershipPermission(
                    workspace_id=workspace.id,
                    membership_id=membership.id,
                    permission="product.price.write",
                ),
                MembershipResourceScopeMode(
                    workspace_id=workspace.id,
                    membership_id=membership.id,
                    resource_type="store",
                    mode=ResourceScopeMode.ALL,
                ),
            ]
        )
        await session.flush()
    else:
        membership, workspace = membership_row

    # 身份/工作区先提交(引导能力用独立连接读取它们)。
    await session.commit()
    capability = await _bootstrap_capabilities(engine, workspace.id)

    # 复位运行面遗留数据与商品域种子(幂等;独立短事务提交)。
    seed_session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with seed_session.begin():
            await _reset_workspace_runtime_rows(seed_session, workspace.id)
            binding = (
                await seed_session.scalars(
                    select(WorkspaceAgentBinding).where(
                        WorkspaceAgentBinding.workspace_id == workspace.id,
                        WorkspaceAgentBinding.agent_version_id
                        == capability["agent_version_id"],
                    )
                )
            ).one_or_none()
            if binding is None:
                seed_session.add(
                    WorkspaceAgentBinding(
                        id=uuid.uuid4(),
                        workspace_id=workspace.id,
                        agent_version_id=capability["agent_version_id"],
                        enabled=True,
                    )
                )
            else:
                binding.enabled = True
            store = (
                await seed_session.scalars(
                    select(Store).where(
                        Store.workspace_id == workspace.id,
                        Store.name == E2E_STORE_NAME,
                    )
                )
            ).one_or_none()
            if store is None:
                store = Store(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    platform=StorePlatform.TAOBAO,
                    external_store_ref=f"e2e-{uuid.uuid4().hex[:12]}",
                    name=E2E_STORE_NAME,
                    aliases=[E2E_STORE_NAME],
                )
                seed_session.add(store)
                await seed_session.flush()
            else:
                store.aliases = [E2E_STORE_NAME]
                await seed_session.flush()
            product = (
                await seed_session.scalars(
                    select(Product).where(
                        Product.workspace_id == workspace.id, Product.sku == E2E_SKU
                    )
                )
            ).one_or_none()
            if product is None:
                product = Product(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    sku=E2E_SKU,
                    title="E2E 演示商品",
                )
                seed_session.add(product)
                await seed_session.flush()
            listing = (
                await seed_session.scalars(
                    select(Listing).where(
                        Listing.workspace_id == workspace.id,
                        Listing.store_id == store.id,
                        Listing.product_id == product.id,
                    )
                )
            ).one_or_none()
            if listing is None:
                listing = Listing(
                    id=uuid.uuid4(),
                    workspace_id=workspace.id,
                    store_id=store.id,
                    product_id=product.id,
                    external_product_ref=f"e2e-item-{uuid.uuid4().hex[:12]}",
                    currency=E2E_CURRENCY,
                    current_price=E2E_INITIAL_PRICE,
                    cost_price=E2E_COST_PRICE,
                )
                seed_session.add(listing)
                await seed_session.flush()
            else:
                # 复位权威价,保证每次 E2E 从 128.00 开始。
                listing.current_price = E2E_INITIAL_PRICE
                listing.cost_price = E2E_COST_PRICE
                listing.currency = E2E_CURRENCY
                await seed_session.flush()
            return E2ESeedView(
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                agent_id=capability["agent_id"],
                store_id=store.id,
                store_name=store.name,
                sku=E2E_SKU,
                listing_id=listing.id,
                initial_price=str(E2E_INITIAL_PRICE),
                login=E2E_LOGIN,
                password=E2E_PASSWORD,
            )
    finally:
        await seed_session.close()


async def _reset_workspace_runtime_rows(session: Any, workspace_id: uuid.UUID) -> None:
    """按外键依赖清空该工作区上次验证遗留的运行面数据(幂等)。"""
    statements: Final[tuple[str, ...]] = (
        "DELETE FROM audit_events WHERE workspace_id = :ws",
        "DELETE FROM agent_events WHERE workspace_id = :ws",
        "DELETE FROM operation_attempts WHERE workspace_id = :ws",
        "UPDATE tool_calls SET operation_id = NULL WHERE workspace_id = :ws",
        "DELETE FROM confirmation_requests WHERE workspace_id = :ws",
        "DELETE FROM input_requests WHERE workspace_id = :ws",
        "DELETE FROM operations WHERE workspace_id = :ws",
        "DELETE FROM tool_calls WHERE workspace_id = :ws",
        "DELETE FROM run_attempts WHERE workspace_id = :ws",
        "DELETE FROM run_jobs WHERE workspace_id = :ws",
        "UPDATE messages SET run_id = NULL WHERE workspace_id = :ws",
        "UPDATE runs SET trigger_message_id = NULL WHERE workspace_id = :ws",
        "DELETE FROM messages WHERE workspace_id = :ws",
        "DELETE FROM run_tool_versions WHERE workspace_id = :ws",
        "DELETE FROM run_skill_versions WHERE workspace_id = :ws",
        "DELETE FROM run_source_versions WHERE workspace_id = :ws",
        "DELETE FROM run_policy_snapshots WHERE workspace_id = :ws",
        "DELETE FROM runs WHERE workspace_id = :ws",
        "DELETE FROM sessions WHERE workspace_id = :ws",
        "DELETE FROM workspace_agent_bindings WHERE workspace_id = :ws",
    )
    for text in statements:
        await session.execute(sa.text(text), {"ws": workspace_id})


__all__ = ["E2ESeedView", "router"]
