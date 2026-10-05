"""Pin AgentVersion on Session.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("agent_version_id", sa.Uuid(), nullable=True))

    # Existing Runs are the strongest evidence of the version used by a Session.
    op.execute(
        sa.text(
            """
            UPDATE sessions AS s
            SET agent_version_id = (
                SELECT r.agent_version_id
                FROM runs AS r
                WHERE r.workspace_id = s.workspace_id AND r.session_id = s.id
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1 FROM runs AS r
                WHERE r.workspace_id = s.workspace_id AND r.session_id = s.id
            )
            """
        )
    )

    # Sessions without Runs predate execution; use their Agent's latest published version.
    op.execute(
        sa.text(
            """
            UPDATE sessions AS s
            SET agent_version_id = (
                SELECT av.id
                FROM agent_versions AS av
                WHERE av.agent_id = s.agent_id AND av.status = 'PUBLISHED'
                ORDER BY av.version DESC, av.id DESC
                LIMIT 1
            )
            WHERE s.agent_version_id IS NULL
            """
        )
    )

    unresolved = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM sessions WHERE agent_version_id IS NULL")
    )
    if unresolved:
        raise RuntimeError(
            f"Cannot pin AgentVersion: {unresolved} existing Session rows have no "
            "Run version or published AgentVersion. Repair those rows before upgrading."
        )

    op.alter_column("sessions", "agent_version_id", nullable=False)
    op.create_foreign_key(
        "fk_sessions_agent_version_id",
        "sessions",
        "agent_versions",
        ["agent_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_sessions_agent_version_id", "sessions", type_="foreignkey")
    op.drop_column("sessions", "agent_version_id")
