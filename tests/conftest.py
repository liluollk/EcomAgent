"""持久化测试夹具。

约定:
- 测试库 URL 必须由 ``RUNTIME_TEST_DATABASE_URL`` 显式给出,不再内置
  ``localhost:5432`` 与固定库名;管理连接由同一 URL 派生,只把库名替换为
  ``postgres``,因此自定义主机/端口的测试实例不会被清理到默认实例上;
- 清理测试库前必须显式设置 ``RUNTIME_TEST_RESET_ALLOWED=1``,且库名以 ``_test``
  结尾;校验失败在任何连接与 ``DROP`` 之前抛出,不依赖全局管理员库的隐式兜底;
- session 级准备:先以 autocommit 连接维护库(postgres),按需 CREATE DATABASE,
  随后清空测试库全部表并使用 Alembic 命令行 API 执行 ``upgrade head``;
- 测试间隔离:每个测试通过未提交事务(连接关闭或显式 rollback 时回滚)隔离,
  需要验证约束冲突的语句包在 SAVEPOINT 中,避免污染外层事务。
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL_ENV = "RUNTIME_TEST_DATABASE_URL"
TEST_DATABASE_RESET_ENV = "RUNTIME_TEST_RESET_ALLOWED"
MAINTENANCE_DATABASE = "postgres"
TEST_DATABASE_SUFFIX = "_test"

# 测试用 JWT 签名密钥。生产路径不再有可用默认值,测试必须显式提供一个。
TEST_JWT_SECRET = "test-secret-for-pytest-only-do-not-use"


@pytest.fixture(scope="session", autouse=True)
def _test_jwt_secret() -> Iterator[None]:
    """给整个测试会话注入签名密钥;结束后恢复进程原值。"""
    previous = os.environ.get("RUNTIME_JWT_SECRET")
    os.environ["RUNTIME_JWT_SECRET"] = TEST_JWT_SECRET
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("RUNTIME_JWT_SECRET", None)
        else:
            os.environ["RUNTIME_JWT_SECRET"] = previous


class UnsafeTestDatabaseError(RuntimeError):
    """测试库目标不满足隔离要求;拒绝发生在任何连接与清表动作之前。"""


@dataclass(frozen=True)
class TestDatabaseTarget:
    """本次 pytest 会话唯一认可的测试库目标。"""

    url: URL
    admin_url: URL
    database: str

    @property
    def url_string(self) -> str:
        """还原成可交给 engine/alembic 的连接串(密码不做脱敏)。"""
        return self.url.render_as_string(hide_password=False)


@dataclass(frozen=True)
class TestDatabaseRules:
    """校验规则的只读句柄,供安全用例通过 fixture 取用。"""

    url_env: str
    reset_env: str
    error_type: type[Exception]
    resolve: Callable[[Mapping[str, str] | None], TestDatabaseTarget]
    derive_admin_url: Callable[[URL], URL]


def derive_admin_url(test_url: URL) -> URL:
    """同实例管理连接:只替换库名,主机、端口与凭据与测试库完全一致。"""
    return test_url.set(database=MAINTENANCE_DATABASE)


def resolve_test_database_target(
    environ: Mapping[str, str] | None = None,
) -> TestDatabaseTarget:
    """解析并校验测试库目标。

    纯函数:只读取环境变量,不建立连接。任何一项校验不通过都抛
    :class:`UnsafeTestDatabaseError`,调用方因此无法在"未通过校验"的状态下
    拿到可用的 URL 去执行截断或删表。
    """
    source = os.environ if environ is None else environ
    raw_url = (source.get(TEST_DATABASE_URL_ENV) or "").strip()
    if not raw_url:
        raise UnsafeTestDatabaseError(
            f"{TEST_DATABASE_URL_ENV} 未设置;请显式指向隔离测试库,"
            "不再回退到 localhost:5432 的固定库名。"
        )

    url = make_url(raw_url)
    database = url.database or ""
    if not database:
        raise UnsafeTestDatabaseError(f"{TEST_DATABASE_URL_ENV} 缺少数据库名: {raw_url}")
    if not database.endswith(TEST_DATABASE_SUFFIX):
        raise UnsafeTestDatabaseError(
            f"拒绝清理数据库 {database!r}:测试库名必须以 {TEST_DATABASE_SUFFIX!r} 结尾。"
        )
    if (source.get(TEST_DATABASE_RESET_ENV) or "").strip() != "1":
        raise UnsafeTestDatabaseError(
            f"拒绝清理数据库 {database!r}:需显式设置 {TEST_DATABASE_RESET_ENV}=1,"
            "该标记只用于明确的隔离测试实例。"
        )

    return TestDatabaseTarget(
        url=url, admin_url=derive_admin_url(url), database=database
    )


def _alembic_config(target: TestDatabaseTarget) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = target.url_string
    return config


async def _create_test_database_if_missing(target: TestDatabaseTarget) -> None:
    admin_engine = create_async_engine(
        target.admin_url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin_engine.connect() as connection:
            exists = await connection.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if exists.scalar() is None:
                await connection.execute(
                    sa.text(f'CREATE DATABASE "{target.database}"')
                )
    finally:
        await admin_engine.dispose()


async def _reset_test_database(target: TestDatabaseTarget) -> None:
    admin_engine = create_async_engine(
        target.admin_url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": target.database},
            )
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(target.url_string, isolation_level="AUTOCOMMIT")
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
            # 业务 Schema(计划 03):随迁移重建,先整体丢弃。
            await connection.execute(
                sa.text("DROP SCHEMA IF EXISTS product_operations CASCADE")
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database_rules() -> TestDatabaseRules:
    """把校验规则交给用例。

    用例不能直接 ``import conftest``:``tests/persistence/conftest.py`` 与本文件
    同名,导入会解析到子目录那份。经 fixture 取用可保证用例断言的实现,与
    ``prepared_test_database`` 实际清库前调用的是同一份。
    """
    return TestDatabaseRules(
        url_env=TEST_DATABASE_URL_ENV,
        reset_env=TEST_DATABASE_RESET_ENV,
        error_type=UnsafeTestDatabaseError,
        resolve=resolve_test_database_target,
        derive_admin_url=derive_admin_url,
    )


@pytest.fixture(scope="session")
def test_database_target() -> TestDatabaseTarget:
    """校验一次,后续所有连接与清表都只用这个目标。"""
    return resolve_test_database_target()


@pytest.fixture(scope="session")
def prepared_test_database(test_database_target: TestDatabaseTarget) -> str:
    """确保测试库存在、清空旧表并应用全部迁移(每次 pytest 会话执行一次)。"""
    asyncio.run(_create_test_database_if_missing(test_database_target))
    asyncio.run(_reset_test_database(test_database_target))
    command.upgrade(_alembic_config(test_database_target), "head")
    return test_database_target.url_string


@pytest.fixture()
async def engine(prepared_test_database: str) -> AsyncIterator[AsyncEngine]:
    """指向已迁移测试库的独立引擎;测试内语句未提交即回滚。"""
    test_engine = create_async_engine(prepared_test_database)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()
