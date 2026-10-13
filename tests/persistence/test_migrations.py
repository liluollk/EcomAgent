"""Migration 与数据库约束测试。

前三个测试来自计划 01 Task 3 原文;后两个验证部分唯一索引的并发约束:
同一 Session 不能有两个活动 Run,同一 Run 不能有两个活动 Job。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError


_INSERT_WORKSPACE = sa.text("INSERT INTO workspaces (id, name) VALUES (:id, :name)")
_INSERT_USER = sa.text(
    "INSERT INTO users (id, login, password_hash) VALUES (:id, :login, :password_hash)"
)
_INSERT_MEMBERSHIP = sa.text(
    "INSERT INTO workspace_memberships (id, workspace_id, user_id, role)"
    " VALUES (:id, :workspace_id, :user_id, 'OPERATOR')"
)
_INSERT_AGENT = sa.text(
    "INSERT INTO agents (id, stable_key, name) VALUES (:id, :stable_key, :name)"
)
_INSERT_AGENT_VERSION = sa.text(
    "INSERT INTO agent_versions (id, agent_id, version, definition_json, definition_hash)"
    " VALUES (:id, :agent_id, 1, '{}', 'hash')"
)
_INSERT_MODEL_CONNECTION = sa.text(
    "INSERT INTO model_connections (id, workspace_id, stable_key, name)"
    " VALUES (:id, :workspace_id, :stable_key, 'model')"
)
_INSERT_MODEL_CONFIG_VERSION = sa.text(
    "INSERT INTO model_config_versions (id, workspace_id, model_connection_id, version,"
    " backend_type, base_url, model, non_secret_options, config_hash)"
    " VALUES (:id, :workspace_id, :model_connection_id, 1, 'OPENAI', 'https://api.example.com/v1',"
    " 'gpt-test', '{}', 'hash')"
)
_INSERT_SESSION = sa.text(
    "INSERT INTO sessions (id, workspace_id, agent_id, agent_version_id, created_by_actor_id,"
    " created_by_membership_id) VALUES (:id, :workspace_id, :agent_id, :agent_version_id,"
    " :created_by_actor_id, :created_by_membership_id)"
)
_INSERT_RUN = sa.text(
    "INSERT INTO runs (id, workspace_id, session_id, created_by_actor_id,"
    " created_by_membership_id, agent_version_id, model_config_version_id, status)"
    " VALUES (:id, :workspace_id, :session_id, :created_by_actor_id,"
    " :created_by_membership_id, :agent_version_id, :model_config_version_id, :status)"
)
_INSERT_RUN_JOB = sa.text(
    "INSERT INTO run_jobs (id, workspace_id, run_id, kind, status, available_at)"
    " VALUES (:id, :workspace_id, :run_id, 'ADVANCE_RUN', :status, :available_at)"
)


async def _seed_session(
    connection: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """创建最小可用的工作区、成员、Agent、Session 与 Run 版本快照依赖。

    返回 (workspace_id, actor_id, membership_id, session_id,
    agent_version_id, model_config_version_id)。
    """
    workspace_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    model_connection_id = uuid.uuid4()
    model_config_version_id = uuid.uuid4()
    await connection.execute(_INSERT_WORKSPACE, {"id": workspace_id, "name": "test-ws"})
    await connection.execute(
        _INSERT_USER,
        {"id": actor_id, "login": f"user-{actor_id}", "password_hash": "hash"},
    )
    await connection.execute(
        _INSERT_MEMBERSHIP,
        {"id": membership_id, "workspace_id": workspace_id, "user_id": actor_id},
    )
    await connection.execute(
        _INSERT_AGENT,
        {"id": agent_id, "stable_key": f"agent-{agent_id}", "name": "test-agent"},
    )
    await connection.execute(
        _INSERT_AGENT_VERSION, {"id": agent_version_id, "agent_id": agent_id}
    )
    await connection.execute(
        _INSERT_MODEL_CONNECTION,
        {"id": model_connection_id, "workspace_id": workspace_id, "stable_key": f"mc-{workspace_id}"},
    )
    await connection.execute(
        _INSERT_MODEL_CONFIG_VERSION,
        {
            "id": model_config_version_id,
            "workspace_id": workspace_id,
            "model_connection_id": model_connection_id,
        },
    )
    await connection.execute(
        _INSERT_SESSION,
        {
            "id": session_id,
            "workspace_id": workspace_id,
            "agent_id": agent_id,
            "agent_version_id": agent_version_id,
            "created_by_actor_id": actor_id,
            "created_by_membership_id": membership_id,
        },
    )
    return (
        workspace_id,
        actor_id,
        membership_id,
        session_id,
        agent_version_id,
        model_config_version_id,
    )


def _run_params(
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    actor_id: uuid.UUID,
    membership_id: uuid.UUID,
    status: str,
    agent_version_id: uuid.UUID,
    model_config_version_id: uuid.UUID,
) -> dict[str, object]:
    return {
        "id": run_id,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "created_by_actor_id": actor_id,
        "created_by_membership_id": membership_id,
        "agent_version_id": agent_version_id,
        "model_config_version_id": model_config_version_id,
        "status": status,
    }


async def test_initial_migration_creates_runtime_tables(engine) -> None:
    async with engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync_conn: set(sa.inspect(sync_conn).get_table_names())
        )
    assert {
        "workspaces",
        "sessions",
        "runs",
        "run_jobs",
        "agent_events",
        "memory_records",
        "integration_outbox",
    } <= names


async def test_initial_migration_enables_vector_extension(engine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
    assert result.scalar_one() == 1


async def test_memory_lifecycle_and_summary_columns_exist(engine) -> None:
    async with engine.connect() as connection:
        memory_columns = await connection.run_sync(
            lambda sync_conn: {
                column["name"]
                for column in sa.inspect(sync_conn).get_columns("memory_records")
            }
        )
        message_columns = await connection.run_sync(
            lambda sync_conn: {
                column["name"] for column in sa.inspect(sync_conn).get_columns("messages")
            }
        )
    assert {
        "subject_key",
        "revision",
        "lifecycle_status",
        "sync_status",
        "expires_at",
    } <= memory_columns
    assert {"covered_from_sequence", "covered_to_sequence", "source_hash"} <= message_columns


async def test_same_session_cannot_have_two_active_runs(engine) -> None:
    """部分唯一索引 uq_active_run_per_session:同一 Session 最多一个活动 Run。"""
    async with engine.connect() as connection:
        (
            workspace_id,
            actor_id,
            membership_id,
            session_id,
            agent_version_id,
            model_config_version_id,
        ) = await _seed_session(connection)
        first_run_id = uuid.uuid4()
        second_run_id = uuid.uuid4()

        await connection.execute(
            _INSERT_RUN,
            _run_params(
                first_run_id,
                workspace_id,
                session_id,
                actor_id,
                membership_id,
                "RUNNING",
                agent_version_id,
                model_config_version_id,
            ),
        )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    _INSERT_RUN,
                    _run_params(
                        second_run_id,
                        workspace_id,
                        session_id,
                        actor_id,
                        membership_id,
                        "QUEUED",
                        agent_version_id,
                        model_config_version_id,
                    ),
                )

        # 活动 Run 进入终态后,同一 Session 允许新的活动 Run。
        await connection.execute(
            sa.text(
                "UPDATE runs SET status = 'COMPLETED', completed_at = now() WHERE id = :id"
            ),
            {"id": first_run_id},
        )
        await connection.execute(
            _INSERT_RUN,
            _run_params(
                second_run_id,
                workspace_id,
                session_id,
                actor_id,
                membership_id,
                "QUEUED",
                agent_version_id,
                model_config_version_id,
            ),
        )
        await connection.rollback()


async def test_same_run_cannot_have_two_active_jobs(engine) -> None:
    """部分唯一索引 uq_active_job_per_run:同一 Run 最多一个 PENDING/LEASED Job。"""
    async with engine.connect() as connection:
        (
            workspace_id,
            actor_id,
            membership_id,
            session_id,
            agent_version_id,
            model_config_version_id,
        ) = await _seed_session(connection)
        run_id = uuid.uuid4()
        first_job_id = uuid.uuid4()
        second_job_id = uuid.uuid4()
        available_at = datetime.now(UTC)

        await connection.execute(
            _INSERT_RUN,
            _run_params(
                run_id,
                workspace_id,
                session_id,
                actor_id,
                membership_id,
                "QUEUED",
                agent_version_id,
                model_config_version_id,
            ),
        )
        await connection.execute(
            _INSERT_RUN_JOB,
            {
                "id": first_job_id,
                "workspace_id": workspace_id,
                "run_id": run_id,
                "status": "PENDING",
                "available_at": available_at,
            },
        )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    _INSERT_RUN_JOB,
                    {
                        "id": second_job_id,
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "status": "PENDING",
                        "available_at": available_at + timedelta(seconds=1),
                    },
                )

        # 活动 Job 完成后,同一 Run 允许新的活动 Job。
        await connection.execute(
            sa.text("UPDATE run_jobs SET status = 'DONE' WHERE id = :id"),
            {"id": first_job_id},
        )
        await connection.execute(
            _INSERT_RUN_JOB,
            {
                "id": second_job_id,
                "workspace_id": workspace_id,
                "run_id": run_id,
                "status": "PENDING",
                "available_at": available_at,
            },
        )
        await connection.rollback()


async def test_p1_migration_creates_inventory_location_tables(engine) -> None:
    """P1 迁移新增 fulfillment_locations 与 inventory_levels 两张表。"""
    async with engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_conn: set(
                sa.inspect(sync_conn).get_table_names(schema="product_operations")
            )
        )
    assert {"fulfillment_locations", "inventory_levels"} <= tables


async def test_p1_migration_migrates_listing_status_constraint(engine) -> None:
    """ck_listings_status 只允许 PUBLISHED/UNPUBLISHED,第一版 ACTIVE/INACTIVE 已移除。"""
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_listings_status'"
            )
        )
        definition = result.scalar_one()
    assert "PUBLISHED" in definition
    assert "UNPUBLISHED" in definition
    assert "ACTIVE" not in definition
    assert "INACTIVE" not in definition


async def test_p1_migration_inventory_levels_enforces_non_negative_quantity(engine) -> None:
    """inventory_levels 的 available_quantity >= 0 由数据库 CHECK 兜底。"""
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_inventory_levels_available_quantity'"
            )
        )
        definition = result.scalar_one()
    assert "available_quantity >= 0" in definition


async def test_product_operations_workspace_ownership_constraints(engine) -> None:
    """0005 把归属关系下沉为数据库约束:组合 FK + 父表 (workspace_id, id) 唯一键。"""
    composite_fks = {
        "fk_listings_store",
        "fk_listings_product",
        "fk_fulfillment_locations_store",
        "fk_inventory_levels_location",
        "fk_inventory_levels_product",
        "fk_price_locks_listing",
    }
    workspace_unique_keys = {
        "uq_stores_workspace_id",
        "uq_products_workspace_id",
        "uq_listings_workspace_id",
        "uq_fulfillment_locations_workspace_id",
    }
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition "
                "FROM pg_constraint WHERE conname = ANY(:names)"
            ),
            {"names": sorted(composite_fks | workspace_unique_keys)},
        )
        definitions = {name: definition for name, definition in rows}

    assert composite_fks <= set(definitions)
    assert workspace_unique_keys <= set(definitions)
    for name in composite_fks:
        definition = definitions[name]
        # 组合外键必须同时约束 workspace_id 与业务父键。
        assert "FOREIGN KEY (workspace_id," in definition
        assert "workspace_id, id)" in definition
    for name in workspace_unique_keys:
        assert "(workspace_id, id)" in definitions[name]
