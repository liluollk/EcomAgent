"""AgentEvent 与审计表(数据模型文档 §10)。

- ``(run_id, sequence_no)`` 唯一(uq_event_sequence_per_run);
- AgentEvent 同时承担持久事件与 WebSocket Outbox;
- Audit Event 只追加,不提供 Update/Delete;
- payload 进入数据库前完成字段级脱敏。
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, ForeignKeyConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from runtime.persistence.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AgentEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Run 的持久事件流;序号由 Run.next_event_sequence 原子递增分配。"""

    __tablename__ = "agent_events"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_agent_events_workspace_id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sequence_no: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_agent_events_session",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_agent_events_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "attempt_id"],
            ["run_attempts.workspace_id", "run_attempts.id"],
            name="fk_agent_events_attempt",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_call_id"],
            ["tool_calls.workspace_id", "tool_calls.id"],
            name="fk_agent_events_tool_call",
        ),
        sa.Index("uq_event_sequence_per_run", "run_id", "sequence_no", unique=True),
        sa.Index(
            "ix_agent_events_workspace_run_sequence",
            "workspace_id",
            "run_id",
            "sequence_no",
        ),
    )


class AuditEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """只追加审计事件;Actor/Action/Target/Outcome 与脱敏 payload。"""

    __tablename__ = "audit_events"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_audit_events_workspace_id"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_audit_events_actor_id"), nullable=False
    )
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(sa.String(64))
    request_id: Mapped[str | None] = mapped_column(sa.String(128))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_audit_events_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "tool_call_id"],
            ["tool_calls.workspace_id", "tool_calls.id"],
            name="fk_audit_events_tool_call",
        ),
        sa.Index(
            "ix_audit_events_workspace_created_at",
            "workspace_id",
            sa.text("created_at DESC"),
        ),
        sa.Index("ix_audit_events_run_created_at", "run_id", "created_at"),
    )
