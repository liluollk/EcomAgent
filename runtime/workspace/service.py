"""Workspace 配置应用服务(计划 04 Task 2)。

应用服务层:读取 Workspace 详情/成员/权限/资源范围,写入执行模式、成员角色、
动作权限、资源范围、模型连接与 Skill/Source/Business Policy 配置。

语义约定:
- 写操作仅限 ``WORKSPACE_OWNER`` 角色(架构文档 §3:Operator 默认不能管理
  成员、模型凭证、Source、Skill、业务规则或 Execution Mode);
- 所有查询与写入都显式携带 ``workspace_id`` 并与 ActorContext 隔离校验;
- 每个写方法在同一事务内通过 :class:`runtime.audit.AuditService` 追加审计
  事件;只 ``flush`` 不 ``commit``,事务边界归调用方(路由)所有;
- Secret 经 ``SecretCipher`` AES-GCM 加密落库,任何返回值只携带掩码,
  明文绝不进入数据库、日志、响应或审计 payload。
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.access.actor import ActorContext
from runtime.audit import AuditService
from runtime.persistence.base import (
    AccountStatus,
    ConfigScope,
    ConfigVersionStatus,
    ExecutionMode,
    MembershipRole,
    ModelBackendType,
    ResourceScopeMode,
)
from runtime.persistence.models.configuration import (
    BusinessPolicy,
    BusinessPolicyVersion,
    ModelConfigVersion,
    ModelConnection,
    Skill,
    SkillVersion,
    Source,
    SourceVersion,
)
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)
from runtime.persistence.models.integrations import Secret
from runtime.security.secrets import SecretCipher, build_secret_binding


SECRET_KIND_MODEL_API_KEY: Final = "MODEL_API_KEY"
MASK_PREFIX: Final = "****"
"""Secret 固定掩码前缀;写响应附明文末 4 位,读取响应只返回固定掩码。"""


class WorkspaceServiceError(Exception):
    """Workspace 配置领域错误基类;由 API 层翻译为统一错误响应。"""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"


class PermissionDenied(WorkspaceServiceError):
    """当前成员角色不足以执行该配置变更。"""

    status_code = 403
    code = "PERMISSION_DENIED"


class NotFoundError(WorkspaceServiceError):
    """目标资源在当前 Workspace 内不存在。"""

    status_code = 404
    code = "NOT_FOUND"


class ConflictError(WorkspaceServiceError):
    """与当前 Workspace 内已有配置冲突(如重复 stable_key)。"""

    status_code = 409
    code = "CONFLICT"


class LastOwnerProtected(WorkspaceServiceError):
    """移除或降级唯一 WorkspaceOwner 被拒绝。"""

    status_code = 409
    code = "LAST_OWNER_REQUIRED"


# ---------------------------------------------------------------------------
# 只读视图(冻结;API 响应模型与领域视图同源)
# ---------------------------------------------------------------------------


class ResourceScopeView(BaseModel):
    """一类资源的范围视图;``SELECTED`` 且无 ID 的类型不返回(等同无权限)。"""

    model_config = ConfigDict(frozen=True)

    mode: str
    resource_ids: list[str]


class WorkspaceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    status: str
    execution_mode: str
    state_version: int
    created_at: datetime
    updated_at: datetime


class ExecutionModeView(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: uuid.UUID
    mode: ExecutionMode
    state_version: int


class MembershipView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str


class MembershipDetailView(MembershipView):
    model_config = ConfigDict(frozen=True)

    permissions: list[str]
    resource_scopes: dict[str, ResourceScopeView]


class MembershipListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    memberships: list[MembershipView]


class ModelConnectionView(BaseModel):
    """模型连接视图;``masked_key`` 只含掩码,绝不携带明文。"""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    stable_key: str
    name: str
    backend_type: str
    base_url: str
    model: str
    masked_key: str | None
    version: int
    updated_at: datetime | None


class ModelConnectionListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_connections: list[ModelConnectionView]


class SkillView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    stable_key: str
    name: str
    latest_version: int | None
    status: str | None


class SkillListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    skills: list[SkillView]


class SourceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    stable_key: str
    kind: str
    name: str
    latest_version: int | None
    status: str | None


class SourceListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    sources: list[SourceView]


class BusinessPolicyView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    stable_key: str
    name: str
    latest_version: int | None
    status: str | None


class BusinessPolicyListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    business_policies: list[BusinessPolicyView]


def _mask_secret(plaintext: str) -> str:
    """写响应掩码:固定前缀 + 明文末 4 位;过短明文只返回固定前缀。"""
    if len(plaintext) < 4:
        return MASK_PREFIX
    return MASK_PREFIX + plaintext[-4:]


def _config_hash(payload: dict[str, Any]) -> str:
    """版本内容的规范化哈希;只包含非 Secret 字段。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkspaceService:
    """Workspace 配置读写应用服务;绑定一个 ActorContext 与一个事务。"""

    def __init__(self, session: AsyncSession, actor: ActorContext) -> None:
        self._session = session
        self._actor = actor
        self._audit = AuditService(
            session, workspace_id=actor.workspace_id, actor_id=actor.actor_id
        )

    # ------------------------------------------------------------------
    # 守卫
    # ------------------------------------------------------------------

    def _require_workspace(self, workspace_id: uuid.UUID) -> None:
        if workspace_id != self._actor.workspace_id:
            raise PermissionDenied("工作区与授权上下文不一致")

    def _require_owner(self) -> None:
        if self._actor.role is not MembershipRole.WORKSPACE_OWNER:
            raise PermissionDenied("只有 WorkspaceOwner 可以修改工作区配置")

    async def _load_membership(
        self, workspace_id: uuid.UUID, membership_id: uuid.UUID
    ) -> WorkspaceMembership:
        membership = (
            await self._session.scalars(
                sa.select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.id == membership_id,
                )
            )
        ).one_or_none()
        if membership is None:
            raise NotFoundError("成员身份不存在")
        return membership

    async def _ensure_not_last_owner(self, membership: WorkspaceMembership) -> None:
        if membership.role is not MembershipRole.WORKSPACE_OWNER:
            return
        owner_count = len(
            (
                await self._session.scalars(
                    sa.select(WorkspaceMembership.id).where(
                        WorkspaceMembership.workspace_id == self._actor.workspace_id,
                        WorkspaceMembership.role == MembershipRole.WORKSPACE_OWNER,
                    )
                )
            ).all()
        )
        if owner_count <= 1:
            raise LastOwnerProtected("工作区必须保留至少一名 WorkspaceOwner")

    async def _membership_detail(
        self, membership: WorkspaceMembership
    ) -> MembershipDetailView:
        permissions = sorted(
            await self._session.scalars(
                sa.select(MembershipPermission.permission).where(
                    MembershipPermission.workspace_id == membership.workspace_id,
                    MembershipPermission.membership_id == membership.id,
                )
            )
        )
        mode_rows = (
            await self._session.scalars(
                sa.select(MembershipResourceScopeMode).where(
                    MembershipResourceScopeMode.workspace_id == membership.workspace_id,
                    MembershipResourceScopeMode.membership_id == membership.id,
                )
            )
        ).all()
        selected: dict[str, list[str]] = {}
        for scope_row in (
            await self._session.scalars(
                sa.select(MembershipResourceScope).where(
                    MembershipResourceScope.workspace_id == membership.workspace_id,
                    MembershipResourceScope.membership_id == membership.id,
                )
            )
        ).all():
            selected.setdefault(scope_row.resource_type, []).append(scope_row.resource_id)
        resource_scopes = {
            mode_row.resource_type: ResourceScopeView(
                mode=mode_row.mode.value,
                resource_ids=sorted(selected.get(mode_row.resource_type, [])),
            )
            for mode_row in mode_rows
            if mode_row.mode is ResourceScopeMode.ALL or selected.get(mode_row.resource_type)
        }
        return MembershipDetailView(
            id=membership.id,
            user_id=membership.user_id,
            role=membership.role.value,
            status=membership.status.value,
            permissions=permissions,
            resource_scopes=resource_scopes,
        )

    # ------------------------------------------------------------------
    # Workspace 读取
    # ------------------------------------------------------------------

    async def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceView:
        self._require_workspace(workspace_id)
        workspace = await self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise NotFoundError("工作区不存在")
        return WorkspaceView(
            id=workspace.id,
            name=workspace.name,
            status=workspace.status.value,
            execution_mode=workspace.execution_mode.value,
            state_version=workspace.state_version,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
        )

    async def get_execution_mode(self, workspace_id: uuid.UUID) -> ExecutionModeView:
        self._require_workspace(workspace_id)
        workspace = await self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise NotFoundError("工作区不存在")
        return ExecutionModeView(
            workspace_id=workspace.id,
            mode=workspace.execution_mode,
            state_version=workspace.state_version,
        )

    # ------------------------------------------------------------------
    # 成员与授权
    # ------------------------------------------------------------------

    async def list_members(
        self, workspace_id: uuid.UUID
    ) -> list[MembershipView]:
        self._require_workspace(workspace_id)
        rows = (
            await self._session.scalars(
                sa.select(WorkspaceMembership)
                .where(WorkspaceMembership.workspace_id == workspace_id)
                .order_by(WorkspaceMembership.created_at, WorkspaceMembership.id)
            )
        ).all()
        return [
            MembershipView(
                id=row.id,
                user_id=row.user_id,
                role=row.role.value,
                status=row.status.value,
            )
            for row in rows
        ]

    async def get_membership(
        self, workspace_id: uuid.UUID, membership_id: uuid.UUID
    ) -> MembershipDetailView:
        self._require_workspace(workspace_id)
        membership = await self._load_membership(workspace_id, membership_id)
        return await self._membership_detail(membership)

    async def update_execution_mode(
        self, workspace_id: uuid.UUID, *, mode: ExecutionMode
    ) -> ExecutionModeView:
        self._require_workspace(workspace_id)
        self._require_owner()
        workspace = await self._session.get(Workspace, workspace_id)
        if workspace is None:
            raise NotFoundError("工作区不存在")
        workspace.execution_mode = mode
        workspace.state_version += 1
        await self._session.flush()
        await self._audit.append(
            "EXECUTION_MODE_UPDATED",
            payload={"mode": mode.value, "state_version": workspace.state_version},
            target_type="WORKSPACE",
            target_id=str(workspace_id),
            request_id=self._actor.request_id,
        )
        return ExecutionModeView(
            workspace_id=workspace.id,
            mode=workspace.execution_mode,
            state_version=workspace.state_version,
        )

    async def add_member(
        self,
        workspace_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        role: MembershipRole,
    ) -> MembershipView:
        self._require_workspace(workspace_id)
        self._require_owner()
        user = (
            await self._session.scalars(
                sa.select(User).where(
                    User.id == user_id, User.status == AccountStatus.ACTIVE
                )
            )
        ).one_or_none()
        if user is None:
            raise NotFoundError("用户不存在或已禁用")
        duplicate = (
            await self._session.scalars(
                sa.select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_id,
                )
            )
        ).one_or_none()
        if duplicate is not None:
            raise ConflictError("该用户已是工作区成员")
        membership = WorkspaceMembership(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
        )
        self._session.add(membership)
        await self._session.flush()
        await self._audit.append(
            "MEMBER_ADDED",
            payload={"user_id": str(user_id), "role": role.value},
            target_type="MEMBERSHIP",
            target_id=str(membership.id),
            request_id=self._actor.request_id,
        )
        return MembershipView(
            id=membership.id,
            user_id=membership.user_id,
            role=membership.role.value,
            status=membership.status.value,
        )

    async def change_member_role(
        self,
        workspace_id: uuid.UUID,
        membership_id: uuid.UUID,
        *,
        role: MembershipRole,
    ) -> MembershipDetailView:
        self._require_workspace(workspace_id)
        self._require_owner()
        membership = await self._load_membership(workspace_id, membership_id)
        if membership.role is not role:
            # 把唯一 Owner 降级等同于移除最后 Owner,同样拒绝。
            await self._ensure_not_last_owner(membership)
            membership.role = role
            await self._session.flush()
            await self._audit.append(
                "MEMBER_ROLE_UPDATED",
                payload={"role": role.value},
                target_type="MEMBERSHIP",
                target_id=str(membership.id),
                request_id=self._actor.request_id,
            )
        return await self._membership_detail(membership)

    async def remove_member(
        self, workspace_id: uuid.UUID, membership_id: uuid.UUID
    ) -> None:
        self._require_workspace(workspace_id)
        self._require_owner()
        membership = await self._load_membership(workspace_id, membership_id)
        await self._ensure_not_last_owner(membership)
        await self._session.delete(membership)
        await self._session.flush()
        await self._audit.append(
            "MEMBER_REMOVED",
            payload={"user_id": str(membership.user_id), "role": membership.role.value},
            target_type="MEMBERSHIP",
            target_id=str(membership.id),
            request_id=self._actor.request_id,
        )

    async def grant_permission(
        self,
        workspace_id: uuid.UUID,
        membership_id: uuid.UUID,
        *,
        permission: str,
    ) -> MembershipDetailView:
        self._require_workspace(workspace_id)
        self._require_owner()
        membership = await self._load_membership(workspace_id, membership_id)
        existing = await self._session.get(
            MembershipPermission, (workspace_id, membership_id, permission)
        )
        if existing is None:
            self._session.add(
                MembershipPermission(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    permission=permission,
                )
            )
            await self._session.flush()
            await self._audit.append(
                "MEMBER_PERMISSION_GRANTED",
                payload={"permission": permission},
                target_type="MEMBERSHIP",
                target_id=str(membership.id),
                request_id=self._actor.request_id,
            )
        return await self._membership_detail(membership)

    async def revoke_permission(
        self,
        workspace_id: uuid.UUID,
        membership_id: uuid.UUID,
        *,
        permission: str,
    ) -> MembershipDetailView:
        self._require_workspace(workspace_id)
        self._require_owner()
        membership = await self._load_membership(workspace_id, membership_id)
        existing = await self._session.get(
            MembershipPermission, (workspace_id, membership_id, permission)
        )
        if existing is None:
            raise NotFoundError("该动作权限未授予")
        await self._session.delete(existing)
        await self._session.flush()
        await self._audit.append(
            "MEMBER_PERMISSION_REVOKED",
            payload={"permission": permission},
            target_type="MEMBERSHIP",
            target_id=str(membership.id),
            request_id=self._actor.request_id,
        )
        return await self._membership_detail(membership)

    async def set_resource_scope(
        self,
        workspace_id: uuid.UUID,
        membership_id: uuid.UUID,
        *,
        resource_type: str,
        mode: ResourceScopeMode,
        resource_ids: list[str],
    ) -> MembershipDetailView:
        self._require_workspace(workspace_id)
        self._require_owner()
        membership = await self._load_membership(workspace_id, membership_id)
        mode_row = await self._session.get(
            MembershipResourceScopeMode, (workspace_id, membership_id, resource_type)
        )
        if mode_row is None:
            self._session.add(
                MembershipResourceScopeMode(
                    workspace_id=workspace_id,
                    membership_id=membership_id,
                    resource_type=resource_type,
                    mode=mode,
                )
            )
        else:
            mode_row.mode = mode
        await self._session.execute(
            sa.delete(MembershipResourceScope).where(
                MembershipResourceScope.workspace_id == workspace_id,
                MembershipResourceScope.membership_id == membership_id,
                MembershipResourceScope.resource_type == resource_type,
            )
        )
        if mode is ResourceScopeMode.SELECTED:
            self._session.add_all(
                [
                    MembershipResourceScope(
                        workspace_id=workspace_id,
                        membership_id=membership_id,
                        resource_type=resource_type,
                        resource_id=resource_id,
                    )
                    for resource_id in sorted(set(resource_ids))
                ]
            )
        await self._session.flush()
        await self._audit.append(
            "MEMBER_RESOURCE_SCOPE_UPDATED",
            payload={
                "resource_type": resource_type,
                "mode": mode.value,
                "resource_ids": sorted(set(resource_ids)),
            },
            target_type="MEMBERSHIP",
            target_id=str(membership.id),
            request_id=self._actor.request_id,
        )
        return await self._membership_detail(membership)

    async def revoke_resource_scope(
        self,
        workspace_id: uuid.UUID,
        membership_id: uuid.UUID,
        *,
        resource_type: str,
    ) -> MembershipDetailView:
        self._require_workspace(workspace_id)
        self._require_owner()
        membership = await self._load_membership(workspace_id, membership_id)
        mode_row = await self._session.get(
            MembershipResourceScopeMode, (workspace_id, membership_id, resource_type)
        )
        if mode_row is None:
            raise NotFoundError("该类资源未配置范围")
        await self._session.delete(mode_row)
        await self._session.execute(
            sa.delete(MembershipResourceScope).where(
                MembershipResourceScope.workspace_id == workspace_id,
                MembershipResourceScope.membership_id == membership_id,
                MembershipResourceScope.resource_type == resource_type,
            )
        )
        await self._session.flush()
        await self._audit.append(
            "MEMBER_RESOURCE_SCOPE_REVOKED",
            payload={"resource_type": resource_type},
            target_type="MEMBERSHIP",
            target_id=str(membership.id),
            request_id=self._actor.request_id,
        )
        return await self._membership_detail(membership)

    # ------------------------------------------------------------------
    # 模型连接(Secret 经 AES-GCM 加密,响应只含掩码)
    # ------------------------------------------------------------------

    async def create_model_connection(
        self,
        workspace_id: uuid.UUID,
        *,
        stable_key: str,
        name: str,
        backend_type: ModelBackendType,
        base_url: str,
        model: str,
        api_key: str,
        non_secret_options: dict[str, Any],
    ) -> ModelConnectionView:
        self._require_workspace(workspace_id)
        self._require_owner()
        duplicate = (
            await self._session.scalars(
                sa.select(ModelConnection.id).where(
                    ModelConnection.workspace_id == workspace_id,
                    ModelConnection.stable_key == stable_key,
                )
            )
        ).one_or_none()
        if duplicate is not None:
            raise ConflictError("模型连接 stable_key 已存在")
        connection = ModelConnection(
            id=uuid.uuid4(), workspace_id=workspace_id, stable_key=stable_key, name=name
        )
        self._session.add(connection)
        await self._session.flush()
        return await self._write_connection_version(
            connection,
            version=1,
            backend_type=backend_type,
            base_url=base_url,
            model=model,
            api_key=api_key,
            non_secret_options=non_secret_options,
            action="MODEL_CONNECTION_CONFIGURED",
        )

    async def update_model_connection(
        self,
        workspace_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        non_secret_options: dict[str, Any] | None = None,
    ) -> ModelConnectionView:
        self._require_workspace(workspace_id)
        self._require_owner()
        connection = (
            await self._session.scalars(
                sa.select(ModelConnection).where(
                    ModelConnection.workspace_id == workspace_id,
                    ModelConnection.id == connection_id,
                )
            )
        ).one_or_none()
        if connection is None:
            raise NotFoundError("模型连接不存在")
        latest = await self._latest_connection_version(workspace_id, connection_id)
        if latest is None:
            raise NotFoundError("模型连接尚无配置版本")
        if name is not None:
            connection.name = name
        # 发布版本不可原地更新:任何变更都创建新版本(数据模型文档 §4)。
        return await self._write_connection_version(
            connection,
            version=latest.version + 1,
            backend_type=latest.backend_type,
            base_url=base_url if base_url is not None else latest.base_url,
            model=model if model is not None else latest.model,
            api_key=api_key,
            non_secret_options=(
                non_secret_options if non_secret_options is not None else dict(latest.non_secret_options)
            ),
            action="MODEL_CONNECTION_UPDATED",
        )

    async def get_model_connection(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> ModelConnectionView:
        self._require_workspace(workspace_id)
        connection = (
            await self._session.scalars(
                sa.select(ModelConnection).where(
                    ModelConnection.workspace_id == workspace_id,
                    ModelConnection.id == connection_id,
                )
            )
        ).one_or_none()
        if connection is None:
            raise NotFoundError("模型连接不存在")
        latest = await self._latest_connection_version(workspace_id, connection_id)
        if latest is None:
            raise NotFoundError("模型连接尚无配置版本")
        return self._connection_view(
            connection,
            version=latest.version,
            base_url=latest.base_url,
            model=latest.model,
            backend_type=latest.backend_type,
            masked_key=MASK_PREFIX if latest.secret_id is not None else None,
            updated_at=None,
        )

    async def list_model_connections(
        self, workspace_id: uuid.UUID
    ) -> list[ModelConnectionView]:
        self._require_workspace(workspace_id)
        connections = (
            await self._session.scalars(
                sa.select(ModelConnection)
                .where(ModelConnection.workspace_id == workspace_id)
                .order_by(ModelConnection.id)
            )
        ).all()
        views: list[ModelConnectionView] = []
        for connection in connections:
            latest = await self._latest_connection_version(workspace_id, connection.id)
            if latest is None:
                continue
            views.append(
                self._connection_view(
                    connection,
                    version=latest.version,
                    base_url=latest.base_url,
                    model=latest.model,
                    backend_type=latest.backend_type,
                    masked_key=MASK_PREFIX if latest.secret_id is not None else None,
                    updated_at=None,
                )
            )
        return views

    async def _latest_connection_version(
        self, workspace_id: uuid.UUID, connection_id: uuid.UUID
    ) -> ModelConfigVersion | None:
        return (
            await self._session.scalars(
                sa.select(ModelConfigVersion)
                .where(
                    ModelConfigVersion.workspace_id == workspace_id,
                    ModelConfigVersion.model_connection_id == connection_id,
                )
                .order_by(ModelConfigVersion.version.desc())
                .limit(1)
            )
        ).one_or_none()

    async def _write_connection_version(
        self,
        connection: ModelConnection,
        *,
        version: int,
        backend_type: ModelBackendType,
        base_url: str,
        model: str,
        api_key: str | None,
        non_secret_options: dict[str, Any],
        action: str,
    ) -> ModelConnectionView:
        """写入一个新的模型配置版本;api_key 提供时创建新的 Secret 行。"""
        secret_row: Secret | None = None
        masked_key: str | None = None
        if api_key is not None:
            cipher = SecretCipher()
            binding = build_secret_binding(
                workspace_id=str(self._actor.workspace_id),
                connection_type=SECRET_KIND_MODEL_API_KEY,
                normalized_endpoint=base_url,
            )
            encrypted = cipher.encrypt(api_key.encode("utf-8"), binding=binding)
            secret_row = Secret(
                id=uuid.uuid4(),
                workspace_id=self._actor.workspace_id,
                kind=SECRET_KIND_MODEL_API_KEY,
                endpoint_binding=base_url,
                key_version=encrypted.key_version,
                nonce=encrypted.nonce,
                ciphertext=encrypted.ciphertext,
                auth_tag=encrypted.tag,
                created_by_actor_id=self._actor.actor_id,
            )
            self._session.add(secret_row)
            await self._session.flush()
            await self._session.refresh(secret_row)  # 载入 server default created_at。
            masked_key = _mask_secret(api_key)
        config_hash = _config_hash(
            {
                "backend_type": backend_type.value,
                "base_url": base_url,
                "model": model,
                "non_secret_options": non_secret_options,
                "secret_id": str(secret_row.id) if secret_row is not None else None,
            }
        )
        version_row = ModelConfigVersion(
            id=uuid.uuid4(),
            workspace_id=self._actor.workspace_id,
            model_connection_id=connection.id,
            version=version,
            backend_type=backend_type,
            base_url=base_url,
            model=model,
            non_secret_options=non_secret_options,
            secret_id=secret_row.id if secret_row is not None else None,
            config_hash=config_hash,
        )
        self._session.add(version_row)
        await self._session.flush()
        await self._audit.append(
            action,
            payload={
                "model_connection_id": str(connection.id),
                "version": version,
                "base_url": base_url,
                "model": model,
                "has_secret": secret_row is not None,
            },
            target_type="MODEL_CONNECTION",
            target_id=str(connection.id),
            request_id=self._actor.request_id,
        )
        return self._connection_view(
            connection,
            version=version,
            base_url=base_url,
            model=model,
            backend_type=backend_type,
            masked_key=masked_key,
            updated_at=secret_row.created_at if secret_row is not None else None,
        )

    @staticmethod
    def _connection_view(
        connection: ModelConnection,
        *,
        version: int,
        base_url: str,
        model: str,
        backend_type: ModelBackendType,
        masked_key: str | None,
        updated_at: datetime | None,
    ) -> ModelConnectionView:
        return ModelConnectionView(
            id=connection.id,
            stable_key=connection.stable_key,
            name=connection.name,
            backend_type=backend_type.value,
            base_url=base_url,
            model=model,
            masked_key=masked_key,
            version=version,
            updated_at=updated_at,
        )

    # ------------------------------------------------------------------
    # Skill / Source / Business Policy(版本化配置,首版只创建 v1)
    # ------------------------------------------------------------------

    async def create_skill(
        self,
        workspace_id: uuid.UUID,
        *,
        stable_key: str,
        name: str,
        content: dict[str, Any],
    ) -> SkillView:
        self._require_workspace(workspace_id)
        self._require_owner()
        duplicate = (
            await self._session.scalars(
                sa.select(Skill.id).where(
                    Skill.workspace_id == workspace_id, Skill.stable_key == stable_key
                )
            )
        ).one_or_none()
        if duplicate is not None:
            raise ConflictError("Skill stable_key 已存在")
        skill = Skill(
            id=uuid.uuid4(),
            scope=ConfigScope.WORKSPACE,
            workspace_id=workspace_id,
            stable_key=stable_key,
            name=name,
        )
        self._session.add(skill)
        await self._session.flush()
        self._session.add(
            SkillVersion(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                skill_id=skill.id,
                version=1,
                status=ConfigVersionStatus.DRAFT,
                content_json=content,
                content_hash=_config_hash(content),
            )
        )
        await self._session.flush()
        await self._audit.append(
            "SKILL_CONFIGURED",
            payload={"stable_key": stable_key, "version": 1},
            target_type="SKILL",
            target_id=str(skill.id),
            request_id=self._actor.request_id,
        )
        return SkillView(
            id=skill.id, stable_key=stable_key, name=name, latest_version=1, status="DRAFT"
        )

    async def list_skills(self, workspace_id: uuid.UUID) -> list[SkillView]:
        self._require_workspace(workspace_id)
        skills = (
            await self._session.scalars(
                sa.select(Skill).where(Skill.workspace_id == workspace_id).order_by(Skill.id)
            )
        ).all()
        latest = await self._latest_versions(
            SkillVersion, workspace_id, "skill_id", [skill.id for skill in skills]
        )
        return [
            SkillView(
                id=skill.id,
                stable_key=skill.stable_key,
                name=skill.name,
                latest_version=latest[skill.id][0],
                status=latest[skill.id][1],
            )
            for skill in skills
        ]

    async def create_source(
        self,
        workspace_id: uuid.UUID,
        *,
        stable_key: str,
        kind: str,
        name: str,
        config: dict[str, Any],
    ) -> SourceView:
        self._require_workspace(workspace_id)
        self._require_owner()
        duplicate = (
            await self._session.scalars(
                sa.select(Source.id).where(
                    Source.workspace_id == workspace_id, Source.stable_key == stable_key
                )
            )
        ).one_or_none()
        if duplicate is not None:
            raise ConflictError("Source stable_key 已存在")
        source = Source(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            stable_key=stable_key,
            kind=kind,
            name=name,
        )
        self._session.add(source)
        await self._session.flush()
        self._session.add(
            SourceVersion(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                source_id=source.id,
                version=1,
                status=ConfigVersionStatus.DRAFT,
                config_json=config,
                config_hash=_config_hash(config),
            )
        )
        await self._session.flush()
        await self._audit.append(
            "SOURCE_CONFIGURED",
            payload={"stable_key": stable_key, "version": 1},
            target_type="SOURCE",
            target_id=str(source.id),
            request_id=self._actor.request_id,
        )
        return SourceView(
            id=source.id,
            stable_key=stable_key,
            kind=kind,
            name=name,
            latest_version=1,
            status="DRAFT",
        )

    async def list_sources(self, workspace_id: uuid.UUID) -> list[SourceView]:
        self._require_workspace(workspace_id)
        sources = (
            await self._session.scalars(
                sa.select(Source).where(Source.workspace_id == workspace_id).order_by(Source.id)
            )
        ).all()
        latest = await self._latest_versions(
            SourceVersion, workspace_id, "source_id", [source.id for source in sources]
        )
        return [
            SourceView(
                id=source.id,
                stable_key=source.stable_key,
                kind=source.kind,
                name=source.name,
                latest_version=latest[source.id][0],
                status=latest[source.id][1],
            )
            for source in sources
        ]

    async def create_business_policy(
        self,
        workspace_id: uuid.UUID,
        *,
        stable_key: str,
        name: str,
        config: dict[str, Any],
    ) -> BusinessPolicyView:
        self._require_workspace(workspace_id)
        self._require_owner()
        duplicate = (
            await self._session.scalars(
                sa.select(BusinessPolicy.id).where(
                    BusinessPolicy.workspace_id == workspace_id,
                    BusinessPolicy.stable_key == stable_key,
                )
            )
        ).one_or_none()
        if duplicate is not None:
            raise ConflictError("Business Policy stable_key 已存在")
        policy = BusinessPolicy(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            stable_key=stable_key,
            name=name,
        )
        self._session.add(policy)
        await self._session.flush()
        self._session.add(
            BusinessPolicyVersion(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                business_policy_id=policy.id,
                version=1,
                status=ConfigVersionStatus.DRAFT,
                config_json=config,
                config_hash=_config_hash(config),
            )
        )
        await self._session.flush()
        await self._audit.append(
            "BUSINESS_POLICY_CONFIGURED",
            payload={"stable_key": stable_key, "version": 1},
            target_type="BUSINESS_POLICY",
            target_id=str(policy.id),
            request_id=self._actor.request_id,
        )
        return BusinessPolicyView(
            id=policy.id,
            stable_key=stable_key,
            name=name,
            latest_version=1,
            status="DRAFT",
        )

    async def list_business_policies(
        self, workspace_id: uuid.UUID
    ) -> list[BusinessPolicyView]:
        self._require_workspace(workspace_id)
        policies = (
            await self._session.scalars(
                sa.select(BusinessPolicy)
                .where(BusinessPolicy.workspace_id == workspace_id)
                .order_by(BusinessPolicy.id)
            )
        ).all()
        latest = await self._latest_versions(
            BusinessPolicyVersion,
            workspace_id,
            "business_policy_id",
            [policy.id for policy in policies],
        )
        return [
            BusinessPolicyView(
                id=policy.id,
                stable_key=policy.stable_key,
                name=policy.name,
                latest_version=latest[policy.id][0],
                status=latest[policy.id][1],
            )
            for policy in policies
        ]

    async def _latest_versions(
        self,
        version_model: type[SkillVersion] | type[SourceVersion] | type[BusinessPolicyVersion],
        workspace_id: uuid.UUID,
        foreign_key: str,
        parent_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[int | None, str | None]]:
        """按 parent 分组取最大版本号与状态;无版本的 parent 映射为 (None, None)。"""
        latest: dict[uuid.UUID, tuple[int, str]] = {}
        if parent_ids:
            foreign_column = getattr(version_model, foreign_key)
            rows = (
                await self._session.scalars(
                    sa.select(version_model).where(
                        version_model.workspace_id == workspace_id,
                        foreign_column.in_(parent_ids),
                    )
                )
            ).all()
            for row in rows:
                any_row = cast(Any, row)
                parent_id = getattr(any_row, foreign_key)
                current = latest.get(parent_id)
                if current is None or any_row.version > current[0]:
                    latest[parent_id] = (any_row.version, any_row.status.value)
        return {parent_id: latest.get(parent_id, (None, None)) for parent_id in parent_ids}
