"""应用门面缓存必须有上限,且淘汰时关闭持有的 Adapter 会话。

缓存键是 ``(workspace, actor, 模型配置版本)``。模型配置每改一次就是新键,
每项各持有一个数据库会话与一个模型客户端。原先只在进程退出时统一关闭,
长期运行的进程会随配置变更和 actor 增加持续累积连接。
"""

from __future__ import annotations

import uuid
from typing import cast

from sqlalchemy.ext.asyncio import AsyncEngine

from apps.model_driver import MAX_CACHED_APPLICATIONS, WorkspaceModelDriver


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _driver() -> WorkspaceModelDriver:
    return WorkspaceModelDriver(engine=cast(AsyncEngine, object()))


async def test_cache_evicts_oldest_and_closes_it() -> None:
    driver = _driver()
    applications = [_Closable() for _ in range(MAX_CACHED_APPLICATIONS + 1)]
    for application in applications:
        driver._applications[(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())] = application

    await driver._evict_overflow()

    assert len(driver._applications) == MAX_CACHED_APPLICATIONS
    assert applications[0].closed is True
    assert all(application.closed is False for application in applications[1:])


async def test_cache_keeps_entry_that_was_used_recently() -> None:
    """命中会把条目移到末尾:淘汰的是最久未使用的那个。"""
    driver = _driver()
    keys = [(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()) for _ in range(3)]
    applications = [_Closable() for _ in keys]
    for key, application in zip(keys, applications, strict=True):
        driver._applications[key] = application

    # 命中最早的那一项,使其变成"最近使用"。
    driver._applications.move_to_end(keys[0])

    # 补足到上限 + 1,只会淘汰一项:最久未使用的是 keys[1]。
    for _ in range(MAX_CACHED_APPLICATIONS - len(keys) + 1):
        driver._applications[(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())] = _Closable()
    await driver._evict_overflow()

    assert applications[0].closed is False
    assert applications[1].closed is True
    assert applications[2].closed is False
