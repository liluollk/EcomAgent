"""Session、Message 与人工交互表(数据模型文档 §5、§8)。

- ``(session_id, sequence_no)`` Message 唯一;
- SESSION_SUMMARY Message 必须同时保存覆盖区间、Summary Schema 版本与来源 Hash;
- 每个 ToolCall 同时最多一个 PENDING Input Request 与一个 PENDING Confirmation Request。
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from runtime.persistence.base import (
    Base,
    ConfirmationRequestStatus,
    InputRequestStatus,
    MessageRole,
    MessageStatus,
    SessionStatus,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    db_enum,
)


class Session(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """会话;首版仅创建者可读写,``active_run_id`` 是查询优化字段。"""

    __tablename__ = "sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_sessions_workspace_id"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", name="fk_sessions_agent_id"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("agent_versions.id", name="fk_sessions_agent_version_id"),
        nullable=False,
    )
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_sessions_created_by_actor_id"), nullable=False
    )
    created_by_membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.String(255))
    status: Mapped[SessionStatus] = mapped_column(
        db_enum(SessionStatus, "ck_sessions_status"),
        nullable=False,
        server_default="ACTIVE",
    )
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(
        # 与 runs 相互引用,迁移中延迟到两张表建立后以 ALTER 添加。
        Uuid,
        ForeignKey("runs.id", name="fk_sessions_active_run_id", use_alter=True),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "created_by_membership_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.id"],
            name="fk_sessions_created_by_membership",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_sessions_workspace_id"),
        sa.Index(
            "ix_sessions_workspace_updated_at",
            "workspace_id",
            sa.text("updated_at DESC"),
        ),
    )


class Message(Base, UUIDPrimaryKeyMixin):
    """会话消息;SESSION_SUMMARY 是内部消息类型,不映射为 Provider System Message。"""

    __tablename__ = "messages"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_messages_workspace_id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    role: Mapped[MessageRole] = mapped_column(
        db_enum(MessageRole, "ck_messages_role"), nullable=False
    )
    status: Mapped[MessageStatus] = mapped_column(
        db_enum(MessageStatus, "ck_messages_status"), nullable=False
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    structured_content: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sequence_no: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    covered_from_sequence: Mapped[int | None] = mapped_column(sa.BigInteger)
    covered_to_sequence: Mapped[int | None] = mapped_column(sa.BigInteger)
    summary_schema_version: Mapped[int | None] = mapped_column(sa.Integer)
    source_hash: Mapped[str | None] = mapped_column(sa.String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_messages_session",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_messages_run",
        ),
        sa.UniqueConstraint("session_id", "sequence_no", name="uq_messages_session_sequence"),
        # 供 runs.trigger_message_id 与 tool_calls.message_id 按 (workspace_id, id) 复合外键引用。
        sa.UniqueConstraint("workspace_id", "id", name="uq_messages_workspace_id"),
        CheckConstraint(
            "(role = 'SESSION_SUMMARY' AND covered_from_sequence IS NOT NULL "
            "AND covered_to_sequence IS NOT NULL AND summary_schema_version IS NOT NULL "
            "AND source_hash IS NOT NULL) "
            "OR (role <> 'SESSION_SUMMARY' AND covered_from_sequence IS NULL "
            "AND covered_to_sequence IS NULL AND summary_schema_version IS NULL "
            "AND source_hash IS NULL)",
            name="ck_messages_summary_coverage",
        ),
    )


class InputRequest(Base, UUIDPrimaryKeyMixin):
    """向用户补充业务字段的请求;一次可要求多个相关字段路径。"""

    __tablename__ = "input_requests"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_input_requests_workspace_id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    field_paths: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    options_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[InputRequestStatus] = mapped_column(
        db_enum(InputRequestStatus, "ck_input_requests_status"), nullable=False
    )
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_input_requests_created_by_actor_id"), nullable=False
    )
    answered_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_input_requests_answered_by_actor_id")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_input_requests_session",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_input_requests_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_call_id"],
            ["tool_calls.workspace_id", "tool_calls.id"],
            name="fk_input_requests_tool_call",
        ),
        sa.Index(
            "uq_pending_input_request_per_tool_call",
            "tool_call_id",
            unique=True,
            postgresql_where=sa.text("status = 'PENDING'"),
        ),
        sa.Index("ix_input_requests_run_status", "run_id", "status"),
        sa.Index("ix_input_requests_expires_at", "expires_at"),
    )


class ConfirmationRequest(Base, UUIDPrimaryKeyMixin):
    """写操作确认请求;指纹与哈希用于检测确认前状态变化。"""

    __tablename__ = "confirmation_requests"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", name="fk_confirmation_requests_workspace_id"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    tool_call_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_confirmation_requests_actor_id"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    normalized_arguments_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    resource_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tool_schema_version: Mapped[int | None] = mapped_column(sa.Integer)
    policy_version: Mapped[int | None] = mapped_column(sa.Integer)
    execution_mode_version: Mapped[int | None] = mapped_column(sa.Integer)
    status: Mapped[ConfirmationRequestStatus] = mapped_column(
        db_enum(ConfirmationRequestStatus, "ck_confirmation_requests_status"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_confirmation_requests_session",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_confirmation_requests_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_call_id"],
            ["tool_calls.workspace_id", "tool_calls.id"],
            name="fk_confirmation_requests_tool_call",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operations.workspace_id", "operations.operation_id"],
            name="fk_confirmation_requests_operation",
        ),
        sa.Index(
            "uq_pending_confirmation_request_per_tool_call",
            "tool_call_id",
            unique=True,
            postgresql_where=sa.text("status = 'PENDING'"),
        ),
        sa.Index("ix_confirmation_requests_run_status", "run_id", "status"),
        sa.Index("ix_confirmation_requests_expires_at", "expires_at"),
    )
