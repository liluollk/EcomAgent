"""Run、执行尝试、Tool 执行与 Job 表(数据模型文档 §5、§6、§7、§9)。

- 部分唯一索引 ``uq_active_run_per_session`` 保证同一 Session 最多一个活动 Run;
- 部分唯一索引 ``uq_active_job_per_run`` 保证同一 Run 最多一个活动 Job;
- Run 配置快照表固定当次运行使用的 Skill/Tool/Source/Policy 版本;
- Workspace 资源关系使用包含 ``workspace_id`` 的复合外键。可被 GLOBAL 作用域
  版本(skill/tool)引用的关系退化为单列外键,因为全局版本 ``workspace_id`` 为空。
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from runtime.persistence.base import (
    ACTIVE_JOB_STATUS_VALUES,
    ACTIVE_RUN_STATUS_VALUES,
    Base,
    OperationAttemptKind,
    OperationAttemptStatus,
    OperationStatus,
    RunAttemptStatus,
    RunJobKind,
    RunJobStatus,
    RunStatus,
    TimestampMixin,
    ToolCallStatus,
    UUIDPrimaryKeyMixin,
    db_enum,
)


_ACTIVE_RUN_WHERE = "status IN ('{}')".format("', '".join(ACTIVE_RUN_STATUS_VALUES))
_ACTIVE_JOB_WHERE = "status IN ('{}')".format("', '".join(ACTIVE_JOB_STATUS_VALUES))


class Run(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次执行;``state_version`` 与 ``next_event_sequence`` 由 CAS 递增。"""

    __tablename__ = "runs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_runs_workspace_id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", name="fk_runs_created_by_actor_id"), nullable=False
    )
    created_by_membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[RunStatus] = mapped_column(
        db_enum(RunStatus, "ck_runs_status"), nullable=False, server_default="QUEUED"
    )
    state_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1")
    )
    next_event_sequence: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("1")
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_versions.id", name="fk_runs_agent_version"), nullable=False
    )
    model_config_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(sa.String(128))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(sa.String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "session_id"],
            ["sessions.workspace_id", "sessions.id"],
            name="fk_runs_session",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "created_by_membership_id"],
            ["workspace_memberships.workspace_id", "workspace_memberships.id"],
            name="fk_runs_created_by_membership",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "trigger_message_id"],
            ["messages.workspace_id", "messages.id"],
            name="fk_runs_trigger_message",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["workspace_id", "model_config_version_id"],
            ["model_config_versions.workspace_id", "model_config_versions.id"],
            name="fk_runs_model_config_version",
        ),
        # 供 messages/tool_calls/run_jobs/agent_events 等按 (workspace_id, run_id) 复合外键引用。
        sa.UniqueConstraint("workspace_id", "id", name="uq_runs_workspace_id"),
        sa.Index(
            "uq_active_run_per_session",
            "session_id",
            unique=True,
            postgresql_where=sa.text(_ACTIVE_RUN_WHERE),
        ),
        sa.Index("ix_runs_workspace_session_created_at", "workspace_id", "session_id", "created_at"),
    )


class RunAttempt(Base, UUIDPrimaryKeyMixin):
    """Run 的单次执行尝试;Job 领取与租约回收都会创建新 Attempt。"""

    __tablename__ = "run_attempts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_run_attempts_workspace_id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(sa.String(255))
    status: Mapped[RunAttemptStatus] = mapped_column(
        db_enum(RunAttemptStatus, "ck_run_attempts_status"),
        nullable=False,
        server_default="RUNNING",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_run_attempts_run",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_run_attempts_workspace_id"),
        sa.UniqueConstraint("run_id", "attempt_no", name="uq_run_attempts_run_attempt"),
    )


class RunSkillVersion(Base):
    """Run 使用的 Skill 版本快照;可引用 GLOBAL Skill(其 workspace_id 为空)。"""

    __tablename__ = "run_skill_versions"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_run_skill_versions_run",
        ),
        ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name="fk_run_skill_versions_skill_version",
        ),
    )


class RunToolVersion(Base):
    """Run 使用的 Tool 版本快照;可引用 GLOBAL Local Tool(其 workspace_id 为空)。"""

    __tablename__ = "run_tool_versions"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tool_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_run_tool_versions_run",
        ),
        ForeignKeyConstraint(
            ["tool_version_id"],
            ["tool_versions.id"],
            name="fk_run_tool_versions_tool_version",
        ),
    )


class RunSourceVersion(Base):
    """Run 使用的 Source 版本快照;Source 属于 Workspace,使用复合外键。"""

    __tablename__ = "run_source_versions"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    source_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_run_source_versions_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_version_id"],
            ["source_versions.workspace_id", "source_versions.id"],
            name="fk_run_source_versions_source_version",
        ),
    )


class RunPolicySnapshot(Base):
    """Run 当时的 Business Policy 视图,用于解释模型看到的规则。"""

    __tablename__ = "run_policy_snapshots"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    policy_key: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    proposed_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    config_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_run_policy_snapshots_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "proposed_version_id"],
            ["business_policy_versions.workspace_id", "business_policy_versions.id"],
            name="fk_run_policy_snapshots_policy_version",
        ),
    )


class ToolCall(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """模型发起的 Tool 调用;只读调用 ``operation_id`` 为空。"""

    __tablename__ = "tool_calls"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_tool_calls_workspace_id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    tool_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tool_versions.id", name="fk_tool_calls_tool_version"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        db_enum(ToolCallStatus, "ck_tool_calls_status"), nullable=False, server_default="PROPOSED"
    )
    raw_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    normalized_arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        # 与 operations 相互引用,复合外键在 __table_args__ 中声明并以 ALTER 延迟创建。
        Uuid
    )
    failure_code: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_tool_calls_run",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "message_id"],
            ["messages.workspace_id", "messages.id"],
            name="fk_tool_calls_message",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id"],
            ["operations.workspace_id", "operations.operation_id"],
            name="fk_tool_calls_operation",
            use_alter=True,
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tool_calls_workspace_id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_tool_calls_run_ordinal"),
    )


class Operation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """逻辑写入操作;进入外部写执行边界时才创建行。"""

    __tablename__ = "operations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_operations_workspace_id"), nullable=False
    )
    tool_call_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    adapter_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    supports_native_idempotency: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    supports_reconciliation: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    expected_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[OperationStatus] = mapped_column(
        db_enum(OperationStatus, "ck_operations_status"),
        nullable=False,
        server_default="PREPARED",
    )
    last_receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "tool_call_id"],
            ["tool_calls.workspace_id", "tool_calls.id"],
            name="fk_operations_tool_call",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_operations_workspace_id"),
        sa.UniqueConstraint("operation_id", name="uq_operations_operation_id"),
        sa.UniqueConstraint("tool_call_id", name="uq_operations_tool_call_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_operations_idempotency_key"),
        # 供 tool_calls/confirmation_requests 按 (workspace_id, operation_id) 复合外键引用。
        sa.UniqueConstraint(
            "workspace_id", "operation_id", name="uq_operations_workspace_operation_id"
        ),
        sa.Index("ix_operations_operation_id", "operation_id"),
        sa.Index("ix_operations_tool_call_id", "tool_call_id"),
    )


class OperationAttempt(Base, UUIDPrimaryKeyMixin):
    """Operation 的单次外部执行或回查尝试。"""

    __tablename__ = "operation_attempts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_operation_attempts_workspace_id"), nullable=False
    )
    operation_row_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    kind: Mapped[OperationAttemptKind] = mapped_column(
        db_enum(OperationAttemptKind, "ck_operation_attempts_kind"), nullable=False
    )
    request_fingerprint: Mapped[str | None] = mapped_column(sa.String(128))
    status: Mapped[OperationAttemptStatus] = mapped_column(
        db_enum(OperationAttemptStatus, "ck_operation_attempts_status"),
        nullable=False,
        server_default="RUNNING",
    )
    external_reference: Mapped[str | None] = mapped_column(sa.String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "operation_row_id"],
            ["operations.workspace_id", "operations.id"],
            name="fk_operation_attempts_operation",
        ),
        sa.UniqueConstraint(
            "operation_row_id", "attempt_no", name="uq_operation_attempts_operation_attempt"
        ),
    )


class RunJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Run 执行 Job;短租约驱动 ADVANCE_RUN 与 RECONCILE_OPERATION。"""

    __tablename__ = "run_jobs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", name="fk_run_jobs_workspace_id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[RunJobKind] = mapped_column(
        db_enum(RunJobKind, "ck_run_jobs_kind"), nullable=False
    )
    status: Mapped[RunJobStatus] = mapped_column(
        db_enum(RunJobStatus, "ck_run_jobs_status"), nullable=False, server_default="PENDING"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("3")
    )
    lease_owner: Mapped[str | None] = mapped_column(sa.String(255))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            name="fk_run_jobs_run",
        ),
        sa.Index(
            "uq_active_job_per_run",
            "run_id",
            unique=True,
            postgresql_where=sa.text(_ACTIVE_JOB_WHERE),
        ),
        sa.Index("ix_run_jobs_status_available_at", "status", "available_at"),
        sa.Index(
            "ix_run_jobs_lease_expires_at",
            "lease_expires_at",
            postgresql_where=sa.text("status = 'LEASED'"),
        ),
    )
