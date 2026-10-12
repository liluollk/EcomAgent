"""Inventory 能力测试夹具(P1 Task 7)。

复用父级 ``tests/agents/product_operations/conftest.py`` 的 ``db_session``
(回滚隔离)。共享支撑(种子、Actor、测试 Gateway)在 ``_support`` 中,此处只暴露
fixture:``prepare`` 会提交读取事务,因此种子行显式提交、用例后按外键倒序清理。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from _support import (
    InventoryEnv,
    RecordingInventoryGateway,
    cleanup_workspace,
    make_actor,
    seed_inventory,
)
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.inventory.service import InventoryService


@pytest.fixture()
def recording_gateway_factory() -> type[RecordingInventoryGateway]:
    """暴露测试 Gateway 类型,供用例按需构造。"""
    return RecordingInventoryGateway


@pytest.fixture()
async def inventory_env(db_session: AsyncSession) -> AsyncIterator[InventoryEnv]:
    """已提交的库存种子 + Actor + Service;用例后按外键倒序清理。"""
    workspace, store, product, listing, location, level = await seed_inventory(db_session)
    await db_session.commit()
    # 捕获标量:Adapter 的失败路径会 rollback 并 expire ORM 对象,后续不能再读行属性。
    workspace_id = workspace.id
    actor = make_actor(workspace_id)
    service = InventoryService(db_session)
    try:
        yield InventoryEnv(
            workspace_id=workspace_id,
            store=store,
            product=product,
            listing=listing,
            location=location,
            level=level,
            actor=actor,
            session=db_session,
            service=service,
        )
    finally:
        await cleanup_workspace(db_session, workspace_id)
