"""Workspace 与身份表(数据模型文档 §3)。

Membership、Scope 与 Permission 使用包含 ``workspace_id`` 的复合外键,
禁止跨 Workspace 引用。所有外键默认 NO ACTION,不依赖级联删除。
"""

import uuid

import sqlalchemy as sa
from sqlalchemy import ForeignKey, ForeignKeyConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from runtime.persistence.base import (
    AccountStatus,
    Base,
    ExecutionMode,
    MembershipRole,
    ResourceScopeMode,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    db_enum,
)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """本地登录用户。"""

    __tablename__ = "users"

    login: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        db_enum(AccountStatus, "ck_users_status"),
        nullable=False,
        server_default="ACTIVE",
    )

    __table_args__ = (sa.UniqueConstraint("login", name="uq_users_login"),)


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """工作区;``state_version`` 用于配置并发控制。"""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        db_enum(AccountStatus, "ck_workspaces_status"),
        nullable=False,
        server_default="ACTIVE",
    )
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        db_enum(ExecutionMode, "ck_workspaces_execution_mode"),
        nullable=False,
        server_default="READ_ONLY",
    )
    state_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1")
    )


class WorkspaceMembership(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """用户在工作区内的成员身份与角色。"""

    __tablename__ = "workspace_memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", name="fk_workspace_memberships_workspace_id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_workspace_memberships_user_id"), nullable=False
    )
    role: Mapped[MembershipRole] = mapped_column(
        db_enum(MembershipRole, "ck_workspace_memberships_role"), nullable=False
    )
    status: Mapped[AccountStatus] = mapped_column(
        db_enum(AccountStatus, "ck_workspace_memberships_status"),
        nullable=False,
        server_default="ACTIVE",
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_workspace_memberships_workspace_user"
        ),
        # 供 Scope/Permission 与业务表按 (workspace_id, membership_id) 复合外键引用。
        sa.UniqueConstraint("workspace_id", "id", name="uq_workspace_memberships_workspace_id"),
    )


class MembershipResourceScopeMode(Base):
    """每类资源的范围模式:ALL 或 SELECTED。"""

    __tablename__ = "membership_resource_scope_modes"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    resource_type: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    mode: Mapped[ResourceScopeMode] = mapped_column(
        db_enum(ResourceScopeMode, "ck_scope_modes_mode"), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.id"],
            name="fk_scope_modes_membership",
        ),
    )


class MembershipResourceScope(Base):
    """SELECTED 模式下允许的资源 ID;空集合表示该类资源无权限。"""

    __tablename__ = "membership_resource_scopes"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    resource_type: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.id"],
            name="fk_scopes_membership",
        ),
    )


class MembershipPermission(Base):
    """成员被授予的动作权限(字符串 Enum,由代码版本管理)。"""

    __tablename__ = "membership_permissions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    permission: Mapped[str] = mapped_column(sa.String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "membership_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.id"],
            name="fk_permissions_membership",
        ),
    )
