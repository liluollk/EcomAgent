"""持久化测试夹具。

约定:
- 测试库 URL 从环境变量 ``RUNTIME_TEST_DATABASE_URL`` 读取,默认连接
  ``agent_runtime_test`` 数据库;
- session 级准备:先以 autocommit 连接维护库(postgres),按需 CREATE DATABASE,
  随后清空测试库全部表并使用 Alembic 命令行 API 执行 ``upgrade head``;
- 测试间隔离:每个测试通过未提交事务(连接关闭或显式 rollback 时回滚)隔离,
  需要验证约束冲突的语句包在 SAVEPOINT 中,避免污染外层事务。
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.environ.get(
    "RUNTIME_TEST_DATABASE_URL",
    "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime_test",
)
ADMIN_DATABASE_URL = "postgresql+asyncpg://runtime:runtime@localhost:5432/postgres"

TEST_DATABASE_NAME = "agent_runtime_test"


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    return config


async def _create_test_database_if_missing() -> None:
    admin_engine = create_async_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            exists = await connection.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DATABASE_NAME},
            )
            if exists.scalar() is None:
                await connection.execute(
                    sa.text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"')
                )
    finally:
        await admin_engine.dispose()


async def _reset_test_database() -> None:
    admin_engine = create_async_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": TEST_DATABASE_NAME},
            )
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                sa.text(
                    "DO $$ DECLARE table_name text; BEGIN "
                    "FOR table_name IN "
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP "
                    "EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(table_name) "
                    "|| ' CASCADE'; END LOOP; END $$;"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def prepared_test_database() -> str:
    """确保测试库存在、清空旧表并应用全部迁移(每次 pytest 会话执行一次)。"""
    asyncio.run(_create_test_database_if_missing())
    asyncio.run(_reset_test_database())
    command.upgrade(_alembic_config(), "head")
    return TEST_DATABASE_URL


@pytest.fixture()
async def engine(prepared_test_database: str) -> AsyncIterator[AsyncEngine]:
    """指向已迁移测试库的独立引擎;测试内语句未提交即回滚。"""
    test_engine = create_async_engine(prepared_test_database)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()
