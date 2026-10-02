"""数据库引擎与连接 URL 助手。

Alembic 与应用共享同一 URL 约定:优先读取环境变量 ``RUNTIME_DATABASE_URL``,
缺省连接本地 docker compose 提供的 ``agent_runtime`` 库。
"""

import os

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


DATABASE_URL_ENV_VAR = "RUNTIME_DATABASE_URL"

DEFAULT_DATABASE_URL = "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime"


def get_database_url() -> str:
    """返回数据库连接 URL,未设置环境变量时使用默认值。"""
    return os.environ.get(DATABASE_URL_ENV_VAR, DEFAULT_DATABASE_URL)


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """创建异步引擎;``pool_pre_ping`` 用于回收被数据库断开的连接。"""
    return create_async_engine(database_url or get_database_url(), pool_pre_ping=True)
