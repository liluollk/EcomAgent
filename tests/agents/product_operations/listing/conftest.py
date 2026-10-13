"""Listing 发布能力测试夹具(P1 Task 8)。

复用父级 ``tests/agents/product_operations/conftest.py`` 的 ``db_session``
(回滚隔离)。共享支撑(种子、Actor、测试 Gateway)在 ``_support`` 中,此处只暴露
fixture:``prepare`` 会提交读取事务,因此种子行显式提交、用例后按外键倒序清理。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from publication_support import (
    PublicationEnv,
    RecordingListingGateway,
    cleanup_workspace,
    make_actor,
    seed_publication,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture()
def recording_gateway_factory() -> type[RecordingListingGateway]:
    """暴露测试 Gateway 类型,供用例按需构造。"""
    return RecordingListingGateway


@pytest.fixture()
async def publication_env(db_session: AsyncSession) -> AsyncIterator[PublicationEnv]:
    """已提交的刊登种子 + Actor;用例后按外键倒序清理。"""
    workspace, store, product, listing, location, level = await seed_publication(db_session)
    await db_session.commit()
    # 捕获标量:Adapter 的 CAS 写入会让 ORM 对象就地刷新,后续不能再读行属性。
    workspace_id = workspace.id
    try:
        yield PublicationEnv(
            workspace_id=workspace_id,
            store=store,
            product=product,
            listing=listing,
            location=location,
            level=level,
            actor=make_actor(workspace_id),
            session=db_session,
        )
    finally:
        await cleanup_workspace(db_session, workspace_id)
