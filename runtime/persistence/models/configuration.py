"""版本化配置表:Agent、Skill、Tool、Source、模型与 Policy(数据模型文档 §4)。

- 发布版本不可原地更新,新内容必须创建新版本;
- GLOBAL 作用域 ``workspace_id`` 必须为空,WORKSPACE 必须非空(CHECK 强制);
- Workspace 资源的版本关系使用包含 ``workspace_id`` 的复合外键。
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from runtime.persistence.base import (
    Base,
    ConfigScope,
    ConfigVersionStatus,
    CreatedAtMixin,
    ModelBackendType,
    ToolEffect,
    UUIDPrimaryKeyMixin,
    db_enum,
)


class Agent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """代码随附的全局 Agent 目录。"""

    __tablename__ = "agents"

    stable_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    __table_args__ = (sa.UniqueConstraint("stable_key", name="uq_agents_stable_key"),)


class AgentVersion(Base, UUIDPrimaryKeyMixin):
    """Agent 不可变版本,``definition_hash`` 用于启动校验。"""

    __tablename__ = "agent_versions"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", name="fk_agent_versions_agent_id"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        db_enum(ConfigVersionStatus, "ck_agent_versions_status"),
        nullable=False,
        server_default="DRAFT",
    )
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
    )


class Skill(Base, UUIDPrimaryKeyMixin):
    """Skill 定义;内置 Skill 全局可用,Workspace Skill 只保存数据化内容。"""

    __tablename__ = "skills"

    scope: Mapped[ConfigScope] = mapped_column(
        db_enum(ConfigScope, "ck_skills_scope"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_skills_workspace_id")
    )
    stable_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(scope = 'GLOBAL' AND workspace_id IS NULL) "
            "OR (scope = 'WORKSPACE' AND workspace_id IS NOT NULL)",
            name="ck_skills_scope_matches_workspace",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_skills_workspace_id"),
        sa.Index(
            "uq_skills_global_stable_key",
            "stable_key",
            unique=True,
            postgresql_where=sa.text("scope = 'GLOBAL'"),
        ),
        sa.Index(
            "uq_skills_workspace_stable_key",
            "workspace_id",
            "stable_key",
            unique=True,
            postgresql_where=sa.text("scope = 'WORKSPACE'"),
        ),
    )


class SkillVersion(Base, UUIDPrimaryKeyMixin):
    """Skill 数据化内容版本。"""

    __tablename__ = "skill_versions"

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        db_enum(ConfigVersionStatus, "ck_skill_versions_status"),
        nullable=False,
        server_default="DRAFT",
    )
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "skill_id"],
            ["skills.workspace_id", "skills.id"],
            name="fk_skill_versions_skill",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_skill_versions_workspace_id"),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
    )


class ToolDefinition(Base, UUIDPrimaryKeyMixin):
    """Local Tool 全局定义;MCP Tool 是 Workspace 资源。"""

    __tablename__ = "tool_definitions"

    scope: Mapped[ConfigScope] = mapped_column(
        db_enum(ConfigScope, "ck_tool_definitions_scope"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_tool_definitions_workspace_id")
    )
    stable_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(scope = 'GLOBAL' AND workspace_id IS NULL) "
            "OR (scope = 'WORKSPACE' AND workspace_id IS NOT NULL)",
            name="ck_tool_definitions_scope_matches_workspace",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tool_definitions_workspace_id"),
        sa.Index(
            "uq_tool_definitions_global_stable_key",
            "stable_key",
            unique=True,
            postgresql_where=sa.text("scope = 'GLOBAL'"),
        ),
        sa.Index(
            "uq_tool_definitions_workspace_stable_key",
            "workspace_id",
            "stable_key",
            unique=True,
            postgresql_where=sa.text("scope = 'WORKSPACE'"),
        ),
    )


class ToolVersion(Base, UUIDPrimaryKeyMixin):
    """Tool 版本;MCP Tool 版本必须关联产生它的 SourceVersion。"""

    __tablename__ = "tool_versions"

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    effect: Mapped[ToolEffect] = mapped_column(
        db_enum(ToolEffect, "ck_tool_versions_effect"), nullable=False
    )
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schema_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    implementation_key: Mapped[str | None] = mapped_column(sa.String(255))
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        db_enum(ConfigVersionStatus, "ck_tool_versions_status"),
        nullable=False,
        server_default="DRAFT",
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "tool_id"],
            ["tool_definitions.workspace_id", "tool_definitions.id"],
            name="fk_tool_versions_tool",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_version_id"],
            ["source_versions.workspace_id", "source_versions.id"],
            name="fk_tool_versions_source_version",
        ),
        CheckConstraint(
            "(workspace_id IS NULL AND source_version_id IS NULL) "
            "OR (workspace_id IS NOT NULL AND source_version_id IS NOT NULL)",
            name="ck_tool_versions_source_matches_scope",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tool_versions_workspace_id"),
        sa.UniqueConstraint("tool_id", "version", name="uq_tool_versions_tool_version"),
    )


class Source(Base, UUIDPrimaryKeyMixin):
    """Workspace 外部集成来源(如 MCP Server)。"""

    __tablename__ = "sources"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_sources_workspace_id"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "stable_key", name="uq_sources_workspace_stable_key"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_sources_workspace_id"),
    )


class SourceVersion(Base, UUIDPrimaryKeyMixin):
    """Source 配置版本;Secret 仅通过 ``secret_id`` 引用。"""

    __tablename__ = "source_versions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    secret_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        db_enum(ConfigVersionStatus, "ck_source_versions_status"),
        nullable=False,
        server_default="DRAFT",
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.id"],
            name="fk_source_versions_source",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "secret_id"],
            ["secrets.workspace_id", "secrets.id"],
            name="fk_source_versions_secret",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_source_versions_workspace_id"),
        sa.UniqueConstraint("source_id", "version", name="uq_source_versions_source_version"),
    )


class ModelConnection(Base, UUIDPrimaryKeyMixin):
    """Workspace 模型连接目录。"""

    __tablename__ = "model_connections"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_model_connections_workspace_id"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "stable_key", name="uq_model_connections_workspace_stable_key"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_model_connections_workspace_id"),
    )


class ModelConfigVersion(Base, UUIDPrimaryKeyMixin):
    """模型配置版本;Base URL 保存规范化结果,Secret 仅通过 ``secret_id`` 引用。"""

    __tablename__ = "model_config_versions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    model_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    backend_type: Mapped[ModelBackendType] = mapped_column(
        db_enum(ModelBackendType, "ck_model_config_versions_backend_type"), nullable=False
    )
    base_url: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    non_secret_options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    secret_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    config_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        db_enum(ConfigVersionStatus, "ck_model_config_versions_status"),
        nullable=False,
        server_default="DRAFT",
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "model_connection_id"],
            ["model_connections.workspace_id", "model_connections.id"],
            name="fk_model_config_versions_connection",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "secret_id"],
            ["secrets.workspace_id", "secrets.id"],
            name="fk_model_config_versions_secret",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_model_config_versions_workspace_id"),
        sa.UniqueConstraint(
            "model_connection_id", "version", name="uq_model_config_versions_connection_version"
        ),
    )


class BusinessPolicy(Base, UUIDPrimaryKeyMixin):
    """Workspace 业务规则目录。"""

    __tablename__ = "business_policies"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_business_policies_workspace_id"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "stable_key", name="uq_business_policies_workspace_stable_key"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_business_policies_workspace_id"),
    )


class BusinessPolicyVersion(Base, UUIDPrimaryKeyMixin):
    """业务规则版本。"""

    __tablename__ = "business_policy_versions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    business_policy_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        db_enum(ConfigVersionStatus, "ck_business_policy_versions_status"),
        nullable=False,
        server_default="DRAFT",
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "business_policy_id"],
            ["business_policies.workspace_id", "business_policies.id"],
            name="fk_business_policy_versions_policy",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_business_policy_versions_workspace_id"),
        sa.UniqueConstraint(
            "business_policy_id", "version", name="uq_business_policy_versions_policy_version"
        ),
    )


class WorkspaceAgentBinding(Base, UUIDPrimaryKeyMixin):
    """Workspace 启用的 Agent 版本。"""

    __tablename__ = "workspace_agent_bindings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", name="fk_workspace_agent_bindings_workspace_id"),
        nullable=False,
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_versions.id", name="fk_workspace_agent_bindings_version"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    effective_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "agent_version_id", name="uq_workspace_agent_bindings"
        ),
    )


class WorkspaceSkillBinding(Base, UUIDPrimaryKeyMixin):
    """Workspace 启用的 Skill 版本。"""

    __tablename__ = "workspace_skill_bindings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", name="fk_workspace_skill_bindings_workspace_id"),
        nullable=False,
    )
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    effective_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "skill_version_id"],
            ["skill_versions.workspace_id", "skill_versions.id"],
            name="fk_workspace_skill_bindings_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "skill_version_id", name="uq_workspace_skill_bindings"
        ),
    )


class WorkspaceToolBinding(Base, UUIDPrimaryKeyMixin):
    """Workspace 启用的 Tool 版本。"""

    __tablename__ = "workspace_tool_bindings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", name="fk_workspace_tool_bindings_workspace_id"),
        nullable=False,
    )
    tool_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    effective_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "tool_version_id"],
            ["tool_versions.workspace_id", "tool_versions.id"],
            name="fk_workspace_tool_bindings_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "tool_version_id", name="uq_workspace_tool_bindings"
        ),
    )


class WorkspaceSourceBinding(Base, UUIDPrimaryKeyMixin):
    """Workspace 启用的 Source 版本。"""

    __tablename__ = "workspace_source_bindings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", name="fk_workspace_source_bindings_workspace_id"),
        nullable=False,
    )
    source_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    effective_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_version_id"],
            ["source_versions.workspace_id", "source_versions.id"],
            name="fk_workspace_source_bindings_version",
        ),
        sa.UniqueConstraint(
            "workspace_id", "source_version_id", name="uq_workspace_source_bindings"
        ),
    )
