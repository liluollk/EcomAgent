"""Workspace 配置 API 请求/响应模型(计划 04 Task 2)。

请求模型全部为 frozen pydantic;响应视图由 ``runtime.workspace.service``
提供(同样为 frozen pydantic,与领域视图同源),在此统一 re-export,
供路由的 ``response_model`` 与测试引用。
"""

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

from runtime.persistence.base import (
    ExecutionMode,
    MembershipRole,
    ModelBackendType,
    ResourceScopeMode,
)
from runtime.workspace.service import (
    BusinessPolicyListResponse,
    BusinessPolicyView,
    ExecutionModeView,
    MembershipDetailView,
    MembershipListResponse,
    MembershipView,
    ModelConnectionListResponse,
    ModelConnectionView,
    ResourceScopeView,
    SkillListResponse,
    SkillView,
    SourceListResponse,
    SourceView,
    WorkspaceView,
)


__all__ = [
    "AddMemberRequest",
    "BusinessPolicyListResponse",
    "BusinessPolicyView",
    "CreateBusinessPolicyRequest",
    "CreateModelConnectionRequest",
    "CreateSkillRequest",
    "CreateSourceRequest",
    "ExecutionModeView",
    "GrantPermissionRequest",
    "MembershipDetailView",
    "MembershipListResponse",
    "MembershipView",
    "ModelConnectionListResponse",
    "ModelConnectionView",
    "ResourceScopeView",
    "SetResourceScopeRequest",
    "SkillListResponse",
    "SkillView",
    "SourceListResponse",
    "SourceView",
    "UpdateExecutionModeRequest",
    "UpdateMemberRoleRequest",
    "UpdateModelConnectionRequest",
    "WorkspaceView",
]


class UpdateExecutionModeRequest(BaseModel):
    """PATCH /execution-mode 请求体。"""

    model_config = ConfigDict(frozen=True)

    mode: ExecutionMode


class AddMemberRequest(BaseModel):
    """POST /memberships 请求体。"""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    role: MembershipRole


class UpdateMemberRoleRequest(BaseModel):
    """PATCH /memberships/{membership_id} 请求体。"""

    model_config = ConfigDict(frozen=True)

    role: MembershipRole


class GrantPermissionRequest(BaseModel):
    """POST /memberships/{membership_id}/permissions 请求体。"""

    model_config = ConfigDict(frozen=True)

    permission: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class SetResourceScopeRequest(BaseModel):
    """PUT /memberships/{membership_id}/resource-scopes 请求体。"""

    model_config = ConfigDict(frozen=True)

    resource_type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    mode: ResourceScopeMode
    resource_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _validate_ids_for_mode(self) -> "SetResourceScopeRequest":
        if self.mode is ResourceScopeMode.SELECTED and not self.resource_ids:
            raise ValueError("SELECTED 范围至少需要一个资源 ID")
        if self.mode is ResourceScopeMode.ALL and self.resource_ids:
            raise ValueError("ALL 范围不能携带资源 ID")
        return self


class CreateModelConnectionRequest(BaseModel):
    """POST /model-connections 请求体;``api_key`` 只落密文、只回掩码。"""

    model_config = ConfigDict(frozen=True)

    stable_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    backend_type: ModelBackendType
    base_url: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    api_key: SecretStr = Field(min_length=1)
    non_secret_options: dict[str, Any] = Field(default_factory=dict)


class UpdateModelConnectionRequest(BaseModel):
    """PATCH /model-connections/{connection_id} 请求体;缺省字段沿用上一版本。"""

    model_config = ConfigDict(frozen=True)

    name: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    base_url: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    model: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    api_key: SecretStr | None = Field(default=None, min_length=1)
    non_secret_options: dict[str, Any] | None = None


class CreateSkillRequest(BaseModel):
    """POST /skills 请求体;创建 Skill 并写入 v1 内容版本。"""

    model_config = ConfigDict(frozen=True)

    stable_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content: dict[str, Any] = Field(default_factory=dict)


class CreateSourceRequest(BaseModel):
    """POST /sources 请求体;创建 Source 并写入 v1 配置版本。"""

    model_config = ConfigDict(frozen=True)

    stable_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    config: dict[str, Any] = Field(default_factory=dict)


class CreateBusinessPolicyRequest(BaseModel):
    """POST /business-policies 请求体;创建业务规则并写入 v1 配置版本。"""

    model_config = ConfigDict(frozen=True)

    stable_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    config: dict[str, Any] = Field(default_factory=dict)
