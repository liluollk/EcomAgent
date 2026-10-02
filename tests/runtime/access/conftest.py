"""Access 测试夹具(计划 01 Task 5)。

提供:
- ``db_session``:与 ``engine`` 同库的 AsyncSession,测试结束回滚保证隔离;
- ``identity_seed``:最小身份种子(owner/operator 两个成员、动作权限与店铺范围);
- ``owner``/``operator``:经 ActorContextBuilder 从当前 PostgreSQL 库态构建的上下文;
- ``authorizer``/``session_access``:被测服务实例。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.access.actor import ActorContext, ActorContextBuilder
from runtime.access.authorization import Authorizer
from runtime.access.session_access import SessionAccess
from runtime.persistence.base import MembershipRole, ResourceScopeMode
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)


REQUEST_ID = "req-access-tests"

# 架构文档 §4.2 权限表全集;owner 拥有全部,operator 仅业务只读/写与 Session。
OWNER_PERMISSIONS = frozenset(
    {
        "workspace.read",
        "workspace.manage",
        "membership.manage",
        "agent.configure",
        "model.configure",
        "policy.configure",
        "session.create",
        "run.read",
        "product.read",
        "product.price.write",
    }
)

OPERATOR_PERMISSIONS = frozenset(
    {"session.create", "run.read", "product.read", "product.price.write"}
)


@dataclass(frozen=True)
class IdentitySeed:
    """最小身份种子行。"""

    workspace: Workspace
    owner_user: User
    operator_user: User
    owner_membership: WorkspaceMembership
    operator_membership: WorkspaceMembership


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
async def identity_seed(db_session: AsyncSession) -> IdentitySeed:
    """创建 workspace + owner/operator 两个成员及权限、店铺范围。"""
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    owner_user = User(id=uuid.uuid4(), login=f"owner-{uuid.uuid4()}", password_hash="test-hash")
    operator_user = User(
        id=uuid.uuid4(), login=f"operator-{uuid.uuid4()}", password_hash="test-hash"
    )
    owner_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=owner_user.id,
        role=MembershipRole.WORKSPACE_OWNER,
    )
    operator_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=operator_user.id,
        role=MembershipRole.OPERATOR,
    )
    db_session.add_all([workspace, owner_user, operator_user])
    await db_session.flush()
    db_session.add_all([owner_membership, operator_membership])
    await db_session.flush()

    db_session.add_all(
        MembershipPermission(
            workspace_id=workspace.id, membership_id=membership.id, permission=permission
        )
        for membership, permissions in (
            (owner_membership, OWNER_PERMISSIONS),
            (operator_membership, OPERATOR_PERMISSIONS),
        )
        for permission in permissions
    )
    db_session.add_all(
        [
            # owner:store 类资源全部对象;operator:仅显式集合内的 store-1。
            MembershipResourceScopeMode(
                workspace_id=workspace.id,
                membership_id=owner_membership.id,
                resource_type="store",
                mode=ResourceScopeMode.ALL,
            ),
            MembershipResourceScopeMode(
                workspace_id=workspace.id,
                membership_id=operator_membership.id,
                resource_type="store",
                mode=ResourceScopeMode.SELECTED,
            ),
            MembershipResourceScope(
                workspace_id=workspace.id,
                membership_id=operator_membership.id,
                resource_type="store",
                resource_id="store-1",
            ),
        ]
    )
    await db_session.flush()
    return IdentitySeed(
        workspace=workspace,
        owner_user=owner_user,
        operator_user=operator_user,
        owner_membership=owner_membership,
        operator_membership=operator_membership,
    )


@pytest.fixture()
async def owner(db_session: AsyncSession, identity_seed: IdentitySeed) -> ActorContext:
    return await ActorContextBuilder(db_session).build(
        actor_id=identity_seed.owner_user.id,
        workspace_id=identity_seed.workspace.id,
        request_id=REQUEST_ID,
    )


@pytest.fixture()
async def operator(db_session: AsyncSession, identity_seed: IdentitySeed) -> ActorContext:
    return await ActorContextBuilder(db_session).build(
        actor_id=identity_seed.operator_user.id,
        workspace_id=identity_seed.workspace.id,
        request_id=REQUEST_ID,
    )


@pytest.fixture()
def authorizer() -> Authorizer:
    return Authorizer()


@pytest.fixture()
def session_access() -> SessionAccess:
    return SessionAccess()
