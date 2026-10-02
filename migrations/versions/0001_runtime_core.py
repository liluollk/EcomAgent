"""runtime core

首版 Schema:身份与 Workspace、版本化配置、Session/Message/Run、
ToolCall/Operation/Request、Job/Event/Audit/Outbox、Secret/Memory,
同时创建 pgvector 扩展(数据模型文档 §15)。

Revision ID: 0001
Revises: 
Create Date: 2026-10-03 00:38:14.038263

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table('agents',
    sa.Column('stable_key', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stable_key', name='uq_agents_stable_key')
    )
    op.create_table('users',
    sa.Column('login', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'DISABLED', name='ck_users_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('login', name='uq_users_login')
    )
    op.create_table('workspaces',
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'DISABLED', name='ck_workspaces_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('execution_mode', sa.Enum('READ_ONLY', 'CONFIRM_WRITES', 'AUTO_EXECUTE', name='ck_workspaces_execution_mode', native_enum=False, create_constraint=True), server_default='READ_ONLY', nullable=False),
    sa.Column('state_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_versions',
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='ck_agent_versions_status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('definition_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('definition_hash', sa.String(length=128), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='fk_agent_versions_agent_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_id', 'version', name='uq_agent_versions_agent_version')
    )
    op.create_table('business_policies',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('stable_key', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_business_policies_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_business_policies_workspace_id'),
    sa.UniqueConstraint('workspace_id', 'stable_key', name='uq_business_policies_workspace_stable_key')
    )
    op.create_table('integration_outbox',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('MEMORY_UPSERT', 'MEMORY_DELETE', 'TRACE_EXPORT', name='ck_integration_outbox_kind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('aggregate_type', sa.String(length=64), nullable=False),
    sa.Column('aggregate_id', sa.String(length=128), nullable=False),
    sa.Column('payload_ref', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'LEASED', 'DONE', 'DEAD', name='ck_integration_outbox_status', native_enum=False, create_constraint=True), server_default='PENDING', nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('attempt_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('lease_owner', sa.String(length=255), nullable=True),
    sa.Column('lease_token', sa.Uuid(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error_code', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_integration_outbox_workspace_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_integration_outbox_lease_expires_at', 'integration_outbox', ['lease_expires_at'], unique=False, postgresql_where=sa.text("status = 'LEASED'"))
    op.create_index('ix_integration_outbox_status_available_at', 'integration_outbox', ['status', 'available_at'], unique=False)
    op.create_table('model_connections',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('stable_key', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_model_connections_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_model_connections_workspace_id'),
    sa.UniqueConstraint('workspace_id', 'stable_key', name='uq_model_connections_workspace_stable_key')
    )
    op.create_table('secrets',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('endpoint_binding', sa.String(length=512), nullable=True),
    sa.Column('key_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('nonce', sa.LargeBinary(), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('auth_tag', sa.LargeBinary(), nullable=True),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_actor_id'], ['users.id'], name='fk_secrets_created_by_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_secrets_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_secrets_workspace_id')
    )
    op.create_table('skills',
    sa.Column('scope', sa.Enum('GLOBAL', 'WORKSPACE', name='ck_skills_scope', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('stable_key', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("(scope = 'GLOBAL' AND workspace_id IS NULL) OR (scope = 'WORKSPACE' AND workspace_id IS NOT NULL)", name='ck_skills_scope_matches_workspace'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_skills_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_skills_workspace_id')
    )
    op.create_index('uq_skills_global_stable_key', 'skills', ['stable_key'], unique=True, postgresql_where=sa.text("scope = 'GLOBAL'"))
    op.create_index('uq_skills_workspace_stable_key', 'skills', ['workspace_id', 'stable_key'], unique=True, postgresql_where=sa.text("scope = 'WORKSPACE'"))
    op.create_table('sources',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('stable_key', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_sources_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_sources_workspace_id'),
    sa.UniqueConstraint('workspace_id', 'stable_key', name='uq_sources_workspace_stable_key')
    )
    op.create_table('tool_definitions',
    sa.Column('scope', sa.Enum('GLOBAL', 'WORKSPACE', name='ck_tool_definitions_scope', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('stable_key', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("(scope = 'GLOBAL' AND workspace_id IS NULL) OR (scope = 'WORKSPACE' AND workspace_id IS NOT NULL)", name='ck_tool_definitions_scope_matches_workspace'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_tool_definitions_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_tool_definitions_workspace_id')
    )
    op.create_index('uq_tool_definitions_global_stable_key', 'tool_definitions', ['stable_key'], unique=True, postgresql_where=sa.text("scope = 'GLOBAL'"))
    op.create_index('uq_tool_definitions_workspace_stable_key', 'tool_definitions', ['workspace_id', 'stable_key'], unique=True, postgresql_where=sa.text("scope = 'WORKSPACE'"))
    op.create_table('workspace_memberships',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('WORKSPACE_OWNER', 'OPERATOR', name='ck_workspace_memberships_role', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'DISABLED', name='ck_workspace_memberships_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_workspace_memberships_user_id'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_workspace_memberships_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_workspace_memberships_workspace_id'),
    sa.UniqueConstraint('workspace_id', 'user_id', name='uq_workspace_memberships_workspace_user')
    )
    op.create_table('business_policy_versions',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('business_policy_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('config_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('config_hash', sa.String(length=128), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='ck_business_policy_versions_status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'business_policy_id'], ['business_policies.workspace_id', 'business_policies.id'], name='fk_business_policy_versions_policy'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_policy_id', 'version', name='uq_business_policy_versions_policy_version'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_business_policy_versions_workspace_id')
    )
    op.create_table('membership_permissions',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('membership_id', sa.Uuid(), nullable=False),
    sa.Column('permission', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'membership_id'], ['workspace_memberships.workspace_id', 'workspace_memberships.id'], name='fk_permissions_membership'),
    sa.PrimaryKeyConstraint('workspace_id', 'membership_id', 'permission')
    )
    op.create_table('membership_resource_scope_modes',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('membership_id', sa.Uuid(), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.Enum('ALL', 'SELECTED', name='ck_scope_modes_mode', native_enum=False, create_constraint=True), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'membership_id'], ['workspace_memberships.workspace_id', 'workspace_memberships.id'], name='fk_scope_modes_membership'),
    sa.PrimaryKeyConstraint('workspace_id', 'membership_id', 'resource_type')
    )
    op.create_table('membership_resource_scopes',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('membership_id', sa.Uuid(), nullable=False),
    sa.Column('resource_type', sa.String(length=64), nullable=False),
    sa.Column('resource_id', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'membership_id'], ['workspace_memberships.workspace_id', 'workspace_memberships.id'], name='fk_scopes_membership'),
    sa.PrimaryKeyConstraint('workspace_id', 'membership_id', 'resource_type', 'resource_id')
    )
    op.create_table('model_config_versions',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('model_connection_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('backend_type', sa.Enum('OPENAI', 'ANTHROPIC', name='ck_model_config_versions_backend_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('base_url', sa.String(length=512), nullable=False),
    sa.Column('model', sa.String(length=255), nullable=False),
    sa.Column('non_secret_options', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('secret_id', sa.Uuid(), nullable=True),
    sa.Column('config_hash', sa.String(length=128), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='ck_model_config_versions_status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'model_connection_id'], ['model_connections.workspace_id', 'model_connections.id'], name='fk_model_config_versions_connection'),
    sa.ForeignKeyConstraint(['workspace_id', 'secret_id'], ['secrets.workspace_id', 'secrets.id'], name='fk_model_config_versions_secret'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_connection_id', 'version', name='uq_model_config_versions_connection_version'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_model_config_versions_workspace_id')
    )
    op.create_table('sessions',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=False),
    sa.Column('created_by_membership_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=True),
    sa.Column('status', sa.Enum('ACTIVE', 'ARCHIVED', name='ck_sessions_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('active_run_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='fk_sessions_agent_id'),
    sa.ForeignKeyConstraint(['created_by_actor_id'], ['users.id'], name='fk_sessions_created_by_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id', 'created_by_membership_id'], ['workspace_memberships.workspace_id', 'workspace_memberships.id'], name='fk_sessions_created_by_membership'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_sessions_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_sessions_workspace_id')
    )
    op.create_index('ix_sessions_workspace_updated_at', 'sessions', ['workspace_id', sa.literal_column('updated_at DESC')], unique=False)
    op.create_table('skill_versions',
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('skill_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='ck_skill_versions_status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('content_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('content_hash', sa.String(length=128), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'skill_id'], ['skills.workspace_id', 'skills.id'], name='fk_skill_versions_skill'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('skill_id', 'version', name='uq_skill_versions_skill_version'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_skill_versions_workspace_id')
    )
    op.create_table('source_versions',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('config_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('config_hash', sa.String(length=128), nullable=False),
    sa.Column('secret_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='ck_source_versions_status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'secret_id'], ['secrets.workspace_id', 'secrets.id'], name='fk_source_versions_secret'),
    sa.ForeignKeyConstraint(['workspace_id', 'source_id'], ['sources.workspace_id', 'sources.id'], name='fk_source_versions_source'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id', 'version', name='uq_source_versions_source_version'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_source_versions_workspace_id')
    )
    op.create_table('workspace_agent_bindings',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('agent_version_id', sa.Uuid(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('effective_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['agent_version_id'], ['agent_versions.id'], name='fk_workspace_agent_bindings_version'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_workspace_agent_bindings_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'agent_version_id', name='uq_workspace_agent_bindings')
    )
    op.create_table('memory_records',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('subject_key', sa.String(length=255), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=128), nullable=False),
    sa.Column('source_type', sa.String(length=64), nullable=False),
    sa.Column('source_id', sa.String(length=128), nullable=True),
    sa.Column('source_session_id', sa.Uuid(), nullable=True),
    sa.Column('revision', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('supersedes_id', sa.Uuid(), nullable=True),
    sa.Column('lifecycle_status', sa.Enum('ACTIVE', 'SUPERSEDED', 'EXPIRED', 'DELETED', name='ck_memory_records_lifecycle_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('sync_status', sa.Enum('PENDING', 'SYNCED', 'FAILED', name='ck_memory_records_sync_status', native_enum=False, create_constraint=True), server_default='PENDING', nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name='fk_memory_records_actor_id'),
    sa.ForeignKeyConstraint(['deleted_by_actor_id'], ['users.id'], name='fk_memory_records_deleted_by_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id', 'source_session_id'], ['sessions.workspace_id', 'sessions.id'], name='fk_memory_records_source_session'),
    sa.ForeignKeyConstraint(['workspace_id', 'supersedes_id'], ['memory_records.workspace_id', 'memory_records.id'], name='fk_memory_records_supersedes'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_memory_records_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_memory_records_workspace_id')
    )
    op.create_index('ix_memory_records_subject', 'memory_records', ['workspace_id', 'actor_id', 'kind', 'subject_key'], unique=False)
    op.create_table('runs',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=False),
    sa.Column('created_by_membership_id', sa.Uuid(), nullable=False),
    sa.Column('trigger_message_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_CONFIRMATION', 'WAITING_RECONCILIATION', 'COMPLETED', 'FAILED', 'CANCELLED', name='ck_runs_status', native_enum=False, create_constraint=True), server_default='QUEUED', nullable=False),
    sa.Column('state_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('next_event_sequence', sa.BigInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('agent_version_id', sa.Uuid(), nullable=False),
    sa.Column('model_config_version_id', sa.Uuid(), nullable=False),
    sa.Column('prompt_version', sa.String(length=128), nullable=True),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['agent_version_id'], ['agent_versions.id'], name='fk_runs_agent_version'),
    sa.ForeignKeyConstraint(['created_by_actor_id'], ['users.id'], name='fk_runs_created_by_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id', 'created_by_membership_id'], ['workspace_memberships.workspace_id', 'workspace_memberships.id'], name='fk_runs_created_by_membership'),
    sa.ForeignKeyConstraint(['workspace_id', 'model_config_version_id'], ['model_config_versions.workspace_id', 'model_config_versions.id'], name='fk_runs_model_config_version'),
    sa.ForeignKeyConstraint(['workspace_id', 'session_id'], ['sessions.workspace_id', 'sessions.id'], name='fk_runs_session'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_runs_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_runs_workspace_id')
    )
    op.create_foreign_key(
        'fk_sessions_active_run_id', 'sessions', 'runs',
        ['active_run_id'], ['id'],
    )
    op.create_index('ix_runs_workspace_session_created_at', 'runs', ['workspace_id', 'session_id', 'created_at'], unique=False)
    op.create_index('uq_active_run_per_session', 'runs', ['session_id'], unique=True, postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_CONFIRMATION', 'WAITING_RECONCILIATION')"))
    op.create_table('tool_versions',
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('tool_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('effect', sa.Enum('READ', 'WRITE', name='ck_tool_versions_effect', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('output_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('schema_hash', sa.String(length=128), nullable=False),
    sa.Column('implementation_key', sa.String(length=255), nullable=True),
    sa.Column('source_version_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Enum('DRAFT', 'PUBLISHED', 'ARCHIVED', name='ck_tool_versions_status', native_enum=False, create_constraint=True), server_default='DRAFT', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('(workspace_id IS NULL AND source_version_id IS NULL) OR (workspace_id IS NOT NULL AND source_version_id IS NOT NULL)', name='ck_tool_versions_source_matches_scope'),
    sa.ForeignKeyConstraint(['workspace_id', 'source_version_id'], ['source_versions.workspace_id', 'source_versions.id'], name='fk_tool_versions_source_version'),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_id'], ['tool_definitions.workspace_id', 'tool_definitions.id'], name='fk_tool_versions_tool'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tool_id', 'version', name='uq_tool_versions_tool_version'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_tool_versions_workspace_id')
    )
    op.create_table('workspace_skill_bindings',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('skill_version_id', sa.Uuid(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('effective_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'skill_version_id'], ['skill_versions.workspace_id', 'skill_versions.id'], name='fk_workspace_skill_bindings_version'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_workspace_skill_bindings_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'skill_version_id', name='uq_workspace_skill_bindings')
    )
    op.create_table('workspace_source_bindings',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('source_version_id', sa.Uuid(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('effective_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'source_version_id'], ['source_versions.workspace_id', 'source_versions.id'], name='fk_workspace_source_bindings_version'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_workspace_source_bindings_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'source_version_id', name='uq_workspace_source_bindings')
    )
    op.create_table('messages',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('role', sa.Enum('USER', 'ASSISTANT', 'TOOL', 'SESSION_SUMMARY', name='ck_messages_role', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('status', sa.Enum('STREAMING', 'COMPLETED', 'ABORTED', name='ck_messages_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('structured_content', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('sequence_no', sa.BigInteger(), nullable=False),
    sa.Column('covered_from_sequence', sa.BigInteger(), nullable=True),
    sa.Column('covered_to_sequence', sa.BigInteger(), nullable=True),
    sa.Column('summary_schema_version', sa.Integer(), nullable=True),
    sa.Column('source_hash', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("(role = 'SESSION_SUMMARY' AND covered_from_sequence IS NOT NULL AND covered_to_sequence IS NOT NULL AND summary_schema_version IS NOT NULL AND source_hash IS NOT NULL) OR (role <> 'SESSION_SUMMARY' AND covered_from_sequence IS NULL AND covered_to_sequence IS NULL AND summary_schema_version IS NULL AND source_hash IS NULL)", name='ck_messages_summary_coverage'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_messages_run'),
    sa.ForeignKeyConstraint(['workspace_id', 'session_id'], ['sessions.workspace_id', 'sessions.id'], name='fk_messages_session'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_messages_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'sequence_no', name='uq_messages_session_sequence'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_messages_workspace_id')
    )
    op.create_foreign_key(
        'fk_runs_trigger_message', 'runs', 'messages',
        ['workspace_id', 'trigger_message_id'], ['workspace_id', 'id'],
    )
    op.create_table('run_attempts',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.String(length=255), nullable=True),
    sa.Column('status', sa.Enum('RUNNING', 'SUCCEEDED', 'FAILED', 'ABANDONED', name='ck_run_attempts_status', native_enum=False, create_constraint=True), server_default='RUNNING', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_run_attempts_run'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_run_attempts_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'attempt_no', name='uq_run_attempts_run_attempt'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_run_attempts_workspace_id')
    )
    op.create_table('run_jobs',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.Enum('ADVANCE_RUN', 'RECONCILE_OPERATION', name='ck_run_jobs_kind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'LEASED', 'DONE', 'DEAD', name='ck_run_jobs_status', native_enum=False, create_constraint=True), server_default='PENDING', nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('attempt_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default=sa.text('3'), nullable=False),
    sa.Column('lease_owner', sa.String(length=255), nullable=True),
    sa.Column('lease_token', sa.Uuid(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error_code', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_run_jobs_run'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_run_jobs_workspace_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_run_jobs_lease_expires_at', 'run_jobs', ['lease_expires_at'], unique=False, postgresql_where=sa.text("status = 'LEASED'"))
    op.create_index('ix_run_jobs_status_available_at', 'run_jobs', ['status', 'available_at'], unique=False)
    op.create_index('uq_active_job_per_run', 'run_jobs', ['run_id'], unique=True, postgresql_where=sa.text("status IN ('PENDING', 'LEASED')"))
    op.create_table('run_policy_snapshots',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('policy_key', sa.String(length=128), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('proposed_version_id', sa.Uuid(), nullable=True),
    sa.Column('config_hash', sa.String(length=128), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'proposed_version_id'], ['business_policy_versions.workspace_id', 'business_policy_versions.id'], name='fk_run_policy_snapshots_policy_version'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_run_policy_snapshots_run'),
    sa.PrimaryKeyConstraint('run_id', 'policy_key')
    )
    op.create_table('run_skill_versions',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('skill_version_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['skill_version_id'], ['skill_versions.id'], name='fk_run_skill_versions_skill_version'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_run_skill_versions_run'),
    sa.PrimaryKeyConstraint('run_id', 'skill_version_id')
    )
    op.create_table('run_source_versions',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('source_version_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_run_source_versions_run'),
    sa.ForeignKeyConstraint(['workspace_id', 'source_version_id'], ['source_versions.workspace_id', 'source_versions.id'], name='fk_run_source_versions_source_version'),
    sa.PrimaryKeyConstraint('run_id', 'source_version_id')
    )
    op.create_table('run_tool_versions',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('tool_version_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['tool_version_id'], ['tool_versions.id'], name='fk_run_tool_versions_tool_version'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_run_tool_versions_run'),
    sa.PrimaryKeyConstraint('run_id', 'tool_version_id')
    )
    op.create_table('workspace_tool_bindings',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('tool_version_id', sa.Uuid(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('effective_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_version_id'], ['tool_versions.workspace_id', 'tool_versions.id'], name='fk_workspace_tool_bindings_version'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_workspace_tool_bindings_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'tool_version_id', name='uq_workspace_tool_bindings')
    )
    op.create_table('tool_calls',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=False),
    sa.Column('tool_version_id', sa.Uuid(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('PROPOSED', 'VALIDATING', 'WAITING_INPUT', 'WAITING_CONFIRMATION', 'EXECUTING', 'SUCCEEDED', 'DENIED', 'FAILED', 'UNKNOWN', 'CANCELLED', name='ck_tool_calls_status', native_enum=False, create_constraint=True), server_default='PROPOSED', nullable=False),
    sa.Column('raw_arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('normalized_arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('operation_id', sa.Uuid(), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tool_version_id'], ['tool_versions.id'], name='fk_tool_calls_tool_version'),
    sa.ForeignKeyConstraint(['workspace_id', 'message_id'], ['messages.workspace_id', 'messages.id'], name='fk_tool_calls_message'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_tool_calls_run'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_tool_calls_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'ordinal', name='uq_tool_calls_run_ordinal'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_tool_calls_workspace_id')
    )
    op.create_table('agent_events',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('sequence_no', sa.BigInteger(), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('schema_version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('attempt_id', sa.Uuid(), nullable=True),
    sa.Column('tool_call_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'attempt_id'], ['run_attempts.workspace_id', 'run_attempts.id'], name='fk_agent_events_attempt'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_agent_events_run'),
    sa.ForeignKeyConstraint(['workspace_id', 'session_id'], ['sessions.workspace_id', 'sessions.id'], name='fk_agent_events_session'),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_call_id'], ['tool_calls.workspace_id', 'tool_calls.id'], name='fk_agent_events_tool_call'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_agent_events_workspace_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agent_events_workspace_run_sequence', 'agent_events', ['workspace_id', 'run_id', 'sequence_no'], unique=False)
    op.create_index('uq_event_sequence_per_run', 'agent_events', ['run_id', 'sequence_no'], unique=True)
    op.create_table('audit_events',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('action', sa.String(length=128), nullable=False),
    sa.Column('target_type', sa.String(length=64), nullable=False),
    sa.Column('target_id', sa.String(length=128), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('reason_code', sa.String(length=64), nullable=True),
    sa.Column('request_id', sa.String(length=128), nullable=True),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('tool_call_id', sa.Uuid(), nullable=True),
    sa.Column('redacted_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name='fk_audit_events_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_audit_events_run'),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_call_id'], ['tool_calls.workspace_id', 'tool_calls.id'], name='fk_audit_events_tool_call'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_audit_events_workspace_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_events_run_created_at', 'audit_events', ['run_id', 'created_at'], unique=False)
    op.create_index('ix_audit_events_workspace_created_at', 'audit_events', ['workspace_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('input_requests',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('tool_call_id', sa.Uuid(), nullable=True),
    sa.Column('field_paths', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('response_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('options_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status', sa.Enum('PENDING', 'ANSWERED', 'EXPIRED', 'CANCELLED', name='ck_input_requests_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('created_by_actor_id', sa.Uuid(), nullable=False),
    sa.Column('answered_by_actor_id', sa.Uuid(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['answered_by_actor_id'], ['users.id'], name='fk_input_requests_answered_by_actor_id'),
    sa.ForeignKeyConstraint(['created_by_actor_id'], ['users.id'], name='fk_input_requests_created_by_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_input_requests_run'),
    sa.ForeignKeyConstraint(['workspace_id', 'session_id'], ['sessions.workspace_id', 'sessions.id'], name='fk_input_requests_session'),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_call_id'], ['tool_calls.workspace_id', 'tool_calls.id'], name='fk_input_requests_tool_call'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_input_requests_workspace_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_input_requests_expires_at', 'input_requests', ['expires_at'], unique=False)
    op.create_index('ix_input_requests_run_status', 'input_requests', ['run_id', 'status'], unique=False)
    op.create_index('uq_pending_input_request_per_tool_call', 'input_requests', ['tool_call_id'], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.create_table('operations',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('tool_call_id', sa.Uuid(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('adapter_key', sa.String(length=255), nullable=False),
    sa.Column('supports_native_idempotency', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('supports_reconciliation', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('expected_state', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status', sa.Enum('PREPARED', 'EXECUTING', 'APPLIED', 'NOT_APPLIED', 'UNKNOWN', 'FAILED', name='ck_operations_status', native_enum=False, create_constraint=True), server_default='PREPARED', nullable=False),
    sa.Column('last_receipt', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_call_id'], ['tool_calls.workspace_id', 'tool_calls.id'], name='fk_operations_tool_call'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_operations_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key', name='uq_operations_idempotency_key'),
    sa.UniqueConstraint('operation_id', name='uq_operations_operation_id'),
    sa.UniqueConstraint('tool_call_id', name='uq_operations_tool_call_id'),
    sa.UniqueConstraint('workspace_id', 'id', name='uq_operations_workspace_id'),
    sa.UniqueConstraint('workspace_id', 'operation_id', name='uq_operations_workspace_operation_id')
    )
    op.create_foreign_key(
        'fk_tool_calls_operation', 'tool_calls', 'operations',
        ['workspace_id', 'operation_id'], ['workspace_id', 'operation_id'],
    )
    op.create_index('ix_operations_operation_id', 'operations', ['operation_id'], unique=False)
    op.create_index('ix_operations_tool_call_id', 'operations', ['tool_call_id'], unique=False)
    op.create_table('confirmation_requests',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('tool_call_id', sa.Uuid(), nullable=False),
    sa.Column('operation_id', sa.Uuid(), nullable=True),
    sa.Column('actor_id', sa.Uuid(), nullable=False),
    sa.Column('fingerprint', sa.String(length=128), nullable=False),
    sa.Column('normalized_arguments_hash', sa.String(length=128), nullable=False),
    sa.Column('resource_versions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('tool_schema_version', sa.Integer(), nullable=True),
    sa.Column('policy_version', sa.Integer(), nullable=True),
    sa.Column('execution_mode_version', sa.Integer(), nullable=True),
    sa.Column('status', sa.Enum('PENDING', 'ACCEPTED', 'REJECTED', 'EXPIRED', 'CANCELLED', name='ck_confirmation_requests_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name='fk_confirmation_requests_actor_id'),
    sa.ForeignKeyConstraint(['workspace_id', 'operation_id'], ['operations.workspace_id', 'operations.operation_id'], name='fk_confirmation_requests_operation'),
    sa.ForeignKeyConstraint(['workspace_id', 'run_id'], ['runs.workspace_id', 'runs.id'], name='fk_confirmation_requests_run'),
    sa.ForeignKeyConstraint(['workspace_id', 'session_id'], ['sessions.workspace_id', 'sessions.id'], name='fk_confirmation_requests_session'),
    sa.ForeignKeyConstraint(['workspace_id', 'tool_call_id'], ['tool_calls.workspace_id', 'tool_calls.id'], name='fk_confirmation_requests_tool_call'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_confirmation_requests_workspace_id'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_confirmation_requests_expires_at', 'confirmation_requests', ['expires_at'], unique=False)
    op.create_index('ix_confirmation_requests_run_status', 'confirmation_requests', ['run_id', 'status'], unique=False)
    op.create_index('uq_pending_confirmation_request_per_tool_call', 'confirmation_requests', ['tool_call_id'], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.create_table('operation_attempts',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('operation_row_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('EXECUTE', 'RECONCILE', name='ck_operation_attempts_kind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=128), nullable=True),
    sa.Column('status', sa.Enum('RUNNING', 'SUCCEEDED', 'FAILED', 'ABANDONED', name='ck_operation_attempts_status', native_enum=False, create_constraint=True), server_default='RUNNING', nullable=False),
    sa.Column('external_reference', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id', 'operation_row_id'], ['operations.workspace_id', 'operations.id'], name='fk_operation_attempts_operation'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name='fk_operation_attempts_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('operation_row_id', 'attempt_no', name='uq_operation_attempts_operation_attempt')
    )


def downgrade() -> None:
    op.drop_table('operation_attempts')
    op.drop_index('uq_pending_confirmation_request_per_tool_call', table_name='confirmation_requests', postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_index('ix_confirmation_requests_run_status', table_name='confirmation_requests')
    op.drop_index('ix_confirmation_requests_expires_at', table_name='confirmation_requests')
    op.drop_table('confirmation_requests')
    op.drop_index('ix_operations_tool_call_id', table_name='operations')
    op.drop_index('ix_operations_operation_id', table_name='operations')
    op.drop_constraint(
        'fk_tool_calls_operation', 'tool_calls', type_='foreignkey'
    )
    op.drop_table('operations')
    op.drop_index('uq_pending_input_request_per_tool_call', table_name='input_requests', postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_index('ix_input_requests_run_status', table_name='input_requests')
    op.drop_index('ix_input_requests_expires_at', table_name='input_requests')
    op.drop_table('input_requests')
    op.drop_index('ix_audit_events_workspace_created_at', table_name='audit_events')
    op.drop_index('ix_audit_events_run_created_at', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('uq_event_sequence_per_run', table_name='agent_events')
    op.drop_index('ix_agent_events_workspace_run_sequence', table_name='agent_events')
    op.drop_table('agent_events')
    op.drop_table('tool_calls')
    op.drop_table('workspace_tool_bindings')
    op.drop_table('run_tool_versions')
    op.drop_table('run_source_versions')
    op.drop_table('run_skill_versions')
    op.drop_table('run_policy_snapshots')
    op.drop_index('uq_active_job_per_run', table_name='run_jobs', postgresql_where=sa.text("status IN ('PENDING', 'LEASED')"))
    op.drop_index('ix_run_jobs_status_available_at', table_name='run_jobs')
    op.drop_index('ix_run_jobs_lease_expires_at', table_name='run_jobs', postgresql_where=sa.text("status = 'LEASED'"))
    op.drop_table('run_jobs')
    op.drop_table('run_attempts')
    op.drop_constraint(
        'fk_runs_trigger_message', 'runs', type_='foreignkey'
    )
    op.drop_table('messages')
    op.drop_table('workspace_source_bindings')
    op.drop_table('workspace_skill_bindings')
    op.drop_table('tool_versions')
    op.drop_index('uq_active_run_per_session', table_name='runs', postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING_INPUT', 'WAITING_CONFIRMATION', 'WAITING_RECONCILIATION')"))
    op.drop_index('ix_runs_workspace_session_created_at', table_name='runs')
    op.drop_constraint(
        'fk_sessions_active_run_id', 'sessions', type_='foreignkey'
    )
    op.drop_table('runs')
    op.drop_index('ix_memory_records_subject', table_name='memory_records')
    op.drop_table('memory_records')
    op.drop_table('workspace_agent_bindings')
    op.drop_table('source_versions')
    op.drop_table('skill_versions')
    op.drop_index('ix_sessions_workspace_updated_at', table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('model_config_versions')
    op.drop_table('membership_resource_scopes')
    op.drop_table('membership_resource_scope_modes')
    op.drop_table('membership_permissions')
    op.drop_table('business_policy_versions')
    op.drop_table('workspace_memberships')
    op.drop_index('uq_tool_definitions_workspace_stable_key', table_name='tool_definitions', postgresql_where=sa.text("scope = 'WORKSPACE'"))
    op.drop_index('uq_tool_definitions_global_stable_key', table_name='tool_definitions', postgresql_where=sa.text("scope = 'GLOBAL'"))
    op.drop_table('tool_definitions')
    op.drop_table('sources')
    op.drop_index('uq_skills_workspace_stable_key', table_name='skills', postgresql_where=sa.text("scope = 'WORKSPACE'"))
    op.drop_index('uq_skills_global_stable_key', table_name='skills', postgresql_where=sa.text("scope = 'GLOBAL'"))
    op.drop_table('skills')
    op.drop_table('secrets')
    op.drop_table('model_connections')
    op.drop_index('ix_integration_outbox_status_available_at', table_name='integration_outbox')
    op.drop_index('ix_integration_outbox_lease_expires_at', table_name='integration_outbox', postgresql_where=sa.text("status = 'LEASED'"))
    op.drop_table('integration_outbox')
    op.drop_table('business_policies')
    op.drop_table('agent_versions')
    op.drop_table('workspaces')
    op.drop_table('users')
    op.drop_table('agents')
    op.execute('DROP EXTENSION IF EXISTS vector')
