"""ActorContext 模型与数据库构建(计划 01 Task 5;架构文档 §2、§4、§6)。

角色、动作权限与资源范围每次从 PostgreSQL 当前状态读取,绝不来自 JWT 或
前端输入;用户、工作区或成员身份缺失/禁用时拒绝构建(架构文档 §6:
Membership 已删除或禁用即拒绝执行)。
"""

from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import AccountStatus, MembershipRole, ResourceScopeMode
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)


class ActorContextError(Exception):
    """无法构建可信 ActorContext:账号、工作区或成员身份缺失或已禁用。"""


class ActorContext(BaseModel):
    """API 校验 JWT 后构造、前端不可覆盖的授权上下文。"""

    model_config = ConfigDict(frozen=True)

    actor_id: UUID
    workspace_id: UUID
    membership_id: UUID
    role: MembershipRole
    permissions: frozenset[str]
    resource_scopes: Mapping[str, ResourceScope]
    request_id: str


class ActorContextBuilder:
    """在调用方事务内从当前库态构建 ActorContext。

    接收调用方(通常为 UnitOfWork 上下文)持有的 ``AsyncSession``,权限
    读取与业务操作共享同一事务与连接,保证读到的是当前 PostgreSQL 状态。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(
        self, *, actor_id: UUID, workspace_id: UUID, request_id: str
    ) -> ActorContext:
        membership = await self._load_membership(actor_id, workspace_id)
        await self._require_active_user(actor_id)
        await self._require_active_workspace(workspace_id)

        permissions = frozenset(
            await self._session.scalars(
                select(MembershipPermission.permission).where(
                    MembershipPermission.workspace_id == workspace_id,
                    MembershipPermission.membership_id == membership.id,
                )
            )
        )
        resource_scopes = await self._load_resource_scopes(workspace_id, membership.id)

        return ActorContext(
            actor_id=actor_id,
            workspace_id=workspace_id,
            membership_id=membership.id,
            role=membership.role,
            permissions=permissions,
            resource_scopes=resource_scopes,
            request_id=request_id,
        )

    async def _load_membership(
        self, actor_id: UUID, workspace_id: UUID
    ) -> WorkspaceMembership:
        membership = (
            await self._session.scalars(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == actor_id,
                    WorkspaceMembership.status == AccountStatus.ACTIVE,
                )
            )
        ).one_or_none()
        if membership is None:
            raise ActorContextError(
                f"workspace {workspace_id} 中不存在 actor {actor_id} 的有效成员身份"
            )
        return membership

    async def _require_active_user(self, actor_id: UUID) -> None:
        user = (
            await self._session.scalars(
                select(User).where(User.id == actor_id, User.status == AccountStatus.ACTIVE)
            )
        ).one_or_none()
        if user is None:
            raise ActorContextError(f"user {actor_id} 不存在或已禁用")

    async def _require_active_workspace(self, workspace_id: UUID) -> None:
        workspace = (
            await self._session.scalars(
                select(Workspace).where(
                    Workspace.id == workspace_id, Workspace.status == AccountStatus.ACTIVE
                )
            )
        ).one_or_none()
        if workspace is None:
            raise ActorContextError(f"workspace {workspace_id} 不存在或已禁用")

    async def _load_resource_scopes(
        self, workspace_id: UUID, membership_id: UUID
    ) -> dict[str, ResourceScope]:
        """组装每类资源的 ResourceScope;类型缺省或 SELECTED 空集合都表示无权限。"""
        mode_rows = (
            await self._session.scalars(
                select(MembershipResourceScopeMode).where(
                    MembershipResourceScopeMode.workspace_id == workspace_id,
                    MembershipResourceScopeMode.membership_id == membership_id,
                )
            )
        ).all()
        selected_ids = await self._load_selected_ids(workspace_id, membership_id)

        resource_scopes: dict[str, ResourceScope] = {}
        for row in mode_rows:
            if row.mode == ResourceScopeMode.ALL:
                resource_scopes[row.resource_type] = ResourceScope(mode="ALL")
                continue
            resource_ids = selected_ids.get(row.resource_type, frozenset())
            if not resource_ids:
                # SELECTED 且无显式 ID = 该类资源无权限;映射中缺省即拒绝。
                continue
            resource_scopes[row.resource_type] = ResourceScope(
                mode="SELECTED", resource_ids=frozenset(resource_ids)
            )
        return resource_scopes

    async def _load_selected_ids(
        self, workspace_id: UUID, membership_id: UUID
    ) -> dict[str, frozenset[str]]:
        scope_rows = (
            await self._session.scalars(
                select(MembershipResourceScope).where(
                    MembershipResourceScope.workspace_id == workspace_id,
                    MembershipResourceScope.membership_id == membership_id,
                )
            )
        ).all()
        selected_ids: dict[str, set[str]] = {}
        for row in scope_rows:
            selected_ids.setdefault(row.resource_type, set()).add(row.resource_id)
        return {resource_type: frozenset(ids) for resource_type, ids in selected_ids.items()}
