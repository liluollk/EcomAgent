"""Mock Commerce Adapter 测试夹具(计划 03 Task 5)。

复用根 conftest(tests/conftest.py)的 ``engine`` fixture:测试库已由
Alembic ``upgrade head`` 准备。Mock Adapter 以 product_operations 的
``listings`` 表为平台权威状态(架构文档 §3),因此与商品域测试共享
同一套未提交事务种子与回滚隔离。
"""

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


# tests/fixtures 不是 pytest 收集路径,显式加入 sys.path 以导入共享夹具。
TESTS_DIR = Path(__file__).resolve().parents[2]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fixtures.product_operations import (
    ProductOperationsSeed,
    ProductOperationsSeeder,
)

from adapters.commerce.mock_adapter import MockCommerceAdapter
from agents.product_operations.persistence.models import Listing
from agents.product_operations.persistence.repository import ProductRepository


@pytest.fixture()
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个测试一个独立 Session;结束时不提交,回滚以保证测试间隔离。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


@pytest.fixture()
def product_seeder(db_session: AsyncSession) -> ProductOperationsSeeder:
    return ProductOperationsSeeder(db_session)


@pytest.fixture()
async def product_seed(product_seeder: ProductOperationsSeeder) -> ProductOperationsSeed:
    """独立 Workspace + Store/Product/Listing/PriceLock 种子。"""
    return await product_seeder.seed()


@pytest.fixture()
def listing(product_seed: ProductOperationsSeed) -> Listing:
    """默认种子的 Listing 行(version/updated_at 等 server default 已载入)。"""
    return product_seed.listing


@pytest.fixture()
def product_repository(db_session: AsyncSession) -> ProductRepository:
    return ProductRepository(db_session)


@pytest.fixture()
def mock_adapter(db_session: AsyncSession) -> MockCommerceAdapter:
    """绑定当前事务 Session 的 Mock 平台 Adapter。"""
    return MockCommerceAdapter(db_session)
