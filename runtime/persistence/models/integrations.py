"""Secret、长期记忆权威记录与外部集成 Outbox(数据模型文档 §11)。

- Memory Record 分离本地 ``lifecycle_status`` 与外部 ``sync_status``,
  保存 ``subject_key/revision/supersedes_id/valid_from/expires_at/deleted_at``;
- Outbox 使用与 RunJob 相同的短租约字段,但其租约不改变 Run 状态;
- Secret 只保存密文,不保存明文。
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, LargeBinary, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from runtime.persistence.base import (
    Base,
    CreatedAtMixin,
    MemoryLifecycleStatus,
    MemorySyncStatus,
    OutboxKind,
    OutboxStatus,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    db_enum,
)


class Secret(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """AES-GCM 加密后的外部凭证;仅通过 id 被 Source/模型配置引用。"""

    __tablename__ = "secrets"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_secrets_workspace_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    endpoint_binding: Mapped[str | None] = mapped_column(sa.String(512))
    key_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_tag: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_secrets_created_by_actor_id"), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # 供 source_versions/model_config_versions 按 (workspace_id, secret_id) 复合外键引用。
        sa.UniqueConstraint("workspace_id", "id", name="uq_secrets_workspace_id"),
    )


class MemoryRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """可审计的记忆权威记录;外部索引(Mem0)不是权威源。"""

    __tablename__ = "memory_records"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_memory_records_workspace_id"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_memory_records_actor_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_id: Mapped[str | None] = mapped_column(sa.String(128))
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lifecycle_status: Mapped[MemoryLifecycleStatus] = mapped_column(
        db_enum(MemoryLifecycleStatus, "ck_memory_records_lifecycle_status"),
        nullable=False,
        server_default="ACTIVE",
    )
    sync_status: Mapped[MemorySyncStatus] = mapped_column(
        db_enum(MemorySyncStatus, "ck_memory_records_sync_status"),
        nullable=False,
        server_default="PENDING",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_memory_records_deleted_by_actor_id")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_memory_records_source_session",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "supersedes_id"],
            ["memory_records.workspace_id", "memory_records.id"],
            name="fk_memory_records_supersedes",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_memory_records_workspace_id"),
        sa.Index(
            "ix_memory_records_subject",
            "workspace_id",
            "actor_id",
            "kind",
            "subject_key",
        ),
    )


class IntegrationOutbox(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """业务事务只写权威状态与 Outbox 行,外部同步在事务外完成。"""

    __tablename__ = "integration_outbox"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_integration_outbox_workspace_id"), nullable=False
    )
    kind: Mapped[OutboxKind] = mapped_column(
        db_enum(OutboxKind, "ck_integration_outbox_kind"), nullable=False
    )
    aggregate_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    payload_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        db_enum(OutboxStatus, "ck_integration_outbox_status"),
        nullable=False,
        server_default="PENDING",
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    lease_owner: Mapped[str | None] = mapped_column(sa.String(255))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        sa.Index("ix_integration_outbox_status_available_at", "status", "available_at"),
        sa.Index(
            "ix_integration_outbox_lease_expires_at",
            "lease_expires_at",
            postgresql_where=sa.text("status = 'LEASED'"),
        ),
    )
