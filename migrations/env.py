"""Alembic 迁移环境。

连接串解析顺序:
1. ``config.attributes["database_url"]``(测试夹具注入);
2. 环境变量 ``RUNTIME_DATABASE_URL``;
3. 本地默认库 ``agent_runtime``。

支持 offline(仅生成 SQL)与 online(异步引擎连接数据库)两种模式。
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from runtime.persistence import (
    database,
    models,  # noqa: F401  确保全部表注册进 metadata
)
from runtime.persistence.base import Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = (
    config.attributes.get("database_url")
    or os.environ.get(database.DATABASE_URL_ENV_VAR)
    or database.DEFAULT_DATABASE_URL
)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """不连接数据库,直接生成 SQL 脚本。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """使用异步引擎在数据库上执行迁移。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
