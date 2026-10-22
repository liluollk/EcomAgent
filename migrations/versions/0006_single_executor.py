"""删除 RunJob 与租约字段,RunAttempt 改记执行器实例

首版执行模型限定「单 API 进程、单 Runtime Executor」(ADR-003)。Run 的推进
不再经过可被多实例抢占的 Job/租约/心跳/fencing,而是在 API 进程内由
``RunExecutor`` 以 ``runs.state_version`` CAS 推进;进程重启时扫描非终态 Run
接管。因此:

- 删除 ``run_jobs`` 表(连同其索引与部分唯一索引);
- ``run_attempts.worker_id`` 改名为 ``executor_id``:语义从"领取 Job 的 Worker"
  变为"一次推进的执行器实例",只用于事后归因,不参与互斥。

``downgrade()`` 恢复旧表的完整结构与字段名。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_active_job_per_run", table_name="run_jobs")
    op.drop_index("ix_run_jobs_status_available_at", table_name="run_jobs")
    op.drop_index("ix_run_jobs_lease_expires_at", table_name="run_jobs")
    op.drop_table("run_jobs")
    op.alter_column(
        "run_attempts", "worker_id", new_column_name="executor_id", existing_type=sa.String(255)
    )


def downgrade() -> None:
    op.alter_column(
        "run_attempts", "executor_id", new_column_name="worker_id", existing_type=sa.String(255)
    )
    op.create_table(
        "run_jobs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ADVANCE_RUN",
                "RECONCILE_OPERATION",
                name="ck_run_jobs_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "LEASED",
                "DONE",
                "DEAD",
                name="ck_run_jobs_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"], name="fk_run_jobs_run"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_run_jobs_workspace_id"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_jobs"),
    )
    op.create_index(
        "uq_active_job_per_run",
        "run_jobs",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'LEASED')"),
    )
    op.create_index(
        "ix_run_jobs_status_available_at", "run_jobs", ["status", "available_at"], unique=False
    )
    op.create_index(
        "ix_run_jobs_lease_expires_at",
        "run_jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'LEASED'"),
    )
