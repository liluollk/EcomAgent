"""Workspace 工作台聚合读取服务(工作台收口设计 §4.2、§5)。

应用层职责:拼装面向工作台的只读页面模型。Runtime 数据经
``WorkspaceService`` 查询;店铺来自商品运营模块(``agents.product_operations``),
仅限当前 Workspace 且当前成员有权访问的店铺。

- ``current_membership`` 来自当前 JWT 用户在该 Workspace 的有效成员记录
  (``ActorContext`` 由 PostgreSQL 当前态构建),不接受前端传入的角色;
- ``agent`` 表示当前 Workspace 启用的商品运营 Agent 版本;没有有效绑定时
  返回 ``None``;多个有效绑定属于配置不变量破坏,抛
  ``AgentConfigurationInvalid``(路由层翻译为 409),不静默选择第一项;
- 配置列表只返回摘要与状态(Model 连接 Key 已脱敏),不含 Secret、Skill
  正文、Source 凭据或业务规则完整内容。
"""

from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StoreStatus
from agents.product_operations.persistence.models import Store
from agents.product_operations.tools.access import authorized_store_ids
from apps.api.services.errors import ApplicationServiceError
from runtime.access.actor import ActorContext
from runtime.persistence.base import ConfigVersionStatus
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    WorkspaceAgentBinding,
)
from runtime.persistence.models.identity import Workspace
from runtime.workspace.service import (
    BusinessPolicyView,
    ModelConnectionView,
    NotFoundError,
    ResourceScopeView,
    SkillView,
    SourceView,
    WorkspaceService,
)


class WorkspaceOverviewError(ApplicationServiceError):
    """Overview 聚合错误基类;由路由层 ``translated()`` 翻译为统一错误响应。"""

    code = "WORKSPACE_OVERVIEW_ERROR"
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AgentConfigurationInvalid(WorkspaceOverviewError):
    """Workspace 启用了多个有效商品 Agent 绑定:配置不变量破坏。"""

    code = "WORKSPACE_AGENT_CONFIGURATION_INVALID"
    status_code = 409


class OverviewWorkspaceView(BaseModel):
    """Workspace 自身字段(设计 §5)。"""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    status: str
    execution_mode: str
    state_version: int


class OverviewMembershipView(BaseModel):
    """当前成员身份;``resource_scopes`` 键为资源类型。"""

    model_config = ConfigDict(frozen=True)

    id: UUID
    role: str
    permissions: list[str]
    resource_scopes: dict[str, ResourceScopeView]


class OverviewStoreView(BaseModel):
    """当前成员有权访问的店铺摘要。"""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    platform: str
    status: str


class OverviewAgentView(BaseModel):
    """当前 Workspace 启用的商品运营 Agent 版本摘要。"""

    model_config = ConfigDict(frozen=True)

    id: UUID
    stable_key: str
    name: str
    version: int
    status: str


class WorkspaceOverviewView(BaseModel):
    """GET /api/workspaces/{workspace_id}/overview 响应(设计 §5)。"""

    model_config = ConfigDict(frozen=True)

    workspace: OverviewWorkspaceView
    current_membership: OverviewMembershipView
    stores: list[OverviewStoreView]
    agent: OverviewAgentView | None
    model_connections: list[ModelConnectionView]
    skills: list[SkillView]
    sources: list[SourceView]
    business_policies: list[BusinessPolicyView]


class WorkspaceOverviewService:
    """聚合读取服务;所有查询显式携带 workspace_id。"""

    def __init__(self, session: AsyncSession, actor: ActorContext) -> None:
        self._session = session
        self._actor = actor
        self._workspaces = WorkspaceService(session, actor)

    async def overview(self, workspace_id: UUID) -> WorkspaceOverviewView:
        workspace_row = (
            await self._session.scalars(
                sa.select(Workspace).where(Workspace.id == workspace_id)
            )
        ).one_or_none()
        # workspace_id 由路由侧 ActorContext 依赖保证为当前成员所在工作区;
        # 不存在或与成员身份不一致一律 404,不泄漏其它工作区的存在性。
        if workspace_row is None or workspace_row.id != self._actor.workspace_id:
            raise NotFoundError("workspace not found")

        membership = await self._workspaces.get_membership(
            workspace_id, self._actor.membership_id
        )
        stores = await self._authorized_stores(workspace_id)
        agent = await self._enabled_agent(workspace_id)
        model_connections = await self._workspaces.list_model_connections(workspace_id)
        skills = await self._workspaces.list_skills(workspace_id)
        sources = await self._workspaces.list_sources(workspace_id)
        business_policies = await self._workspaces.list_business_policies(workspace_id)

        return WorkspaceOverviewView(
            workspace=OverviewWorkspaceView(
                id=workspace_row.id,
                name=workspace_row.name,
                status=workspace_row.status.value,
                execution_mode=workspace_row.execution_mode.value,
                state_version=workspace_row.state_version,
            ),
            current_membership=OverviewMembershipView(
                id=membership.id,
                role=membership.role,
                permissions=list(membership.permissions),
                resource_scopes=dict(membership.resource_scopes),
            ),
            stores=stores,
            agent=agent,
            model_connections=model_connections,
            skills=skills,
            sources=sources,
            business_policies=business_policies,
        )

    async def _authorized_stores(self, workspace_id: UUID) -> list[OverviewStoreView]:
        """当前 Workspace 内、当前成员有权访问的有效店铺。"""
        statement = (
            sa.select(Store)
            .where(Store.workspace_id == workspace_id)
            .where(Store.status == StoreStatus.ACTIVE)
            .order_by(Store.created_at, Store.id)
        )
        authorized = authorized_store_ids(self._actor)
        if authorized is not None:
            if not authorized:
                return []
            statement = statement.where(
                Store.id.in_([UUID(value) for value in sorted(authorized)])
            )
        rows = (await self._session.scalars(statement)).all()
        return [
            OverviewStoreView(
                id=store.id,
                name=store.name,
                platform=store.platform.value,
                status=store.status.value,
            )
            for store in rows
        ]

    async def _enabled_agent(self, workspace_id: UUID) -> OverviewAgentView | None:
        """启用的商品运营 Agent;0 个 → None,多于 1 个 → 配置不变量破坏。"""
        rows = (
            await self._session.execute(
                sa.select(WorkspaceAgentBinding, AgentVersion, Agent)
                .join(
                    AgentVersion,
                    AgentVersion.id == WorkspaceAgentBinding.agent_version_id,
                )
                .join(Agent, Agent.id == AgentVersion.agent_id)
                .where(WorkspaceAgentBinding.workspace_id == workspace_id)
                .where(WorkspaceAgentBinding.enabled.is_(True))
                .where(WorkspaceAgentBinding.effective_at <= sa.func.now())
                .where(AgentVersion.status == ConfigVersionStatus.PUBLISHED)
                .order_by(WorkspaceAgentBinding.effective_at, WorkspaceAgentBinding.id)
            )
        ).all()
        if not rows:
            return None
        if len(rows) > 1:
            raise AgentConfigurationInvalid(
                "workspace has multiple enabled agent bindings"
            )
        _, version, agent = rows[0]
        return OverviewAgentView(
            id=agent.id,
            stable_key=agent.stable_key,
            name=agent.name,
            version=version.version,
            status=version.status.value,
        )


__all__ = [
    "AgentConfigurationInvalid",
    "OverviewAgentView",
    "OverviewMembershipView",
    "OverviewStoreView",
    "WorkspaceOverviewError",
    "WorkspaceOverviewService",
    "WorkspaceOverviewView",
]
