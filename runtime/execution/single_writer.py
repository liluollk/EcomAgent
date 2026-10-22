"""进程级单写者锁(执行模型边界,见 ADR-003)。

首版执行模型限定「单 API 进程、单 Runtime Executor」:Run 的推进不再经过
可被多进程抢占的 Job/租约,而是在 API 进程内由 :class:`run_executor.RunExecutor`
直接推进。该约束由本锁在启动时断言,而不是只写在部署文档里:

- 启动时尝试获取 PostgreSQL advisory lock;获取失败说明同一数据库上已有
  另一个执行器在运行,直接 fail-fast,不进入"降级只读"或"继续运行";
- 锁挂在一条独立连接上,连接关闭即释放,进程崩溃时由 PostgreSQL 回收;
- 这不是 per-task 租约:每个进程生命周期只竞争一次,不参与任务调度。

advisory lock 的键是固定常量,因此同一数据库的所有执行器实例共用一把锁。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


ADVISORY_LOCK_KEY = 0x52554E45
"""``RUNX`` 的 ASCII 编码;固定键保证同库执行器互斥。"""


class SingleWriterViolation(RuntimeError):
    """同一数据库上已存在另一个 Run 执行器。"""


class SingleWriterLock:
    """持有 PostgreSQL advisory lock 的执行器单例断言。"""

    def __init__(self, engine: AsyncEngine, *, key: int = ADVISORY_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._connection: AsyncConnection | None = None

    @property
    def held(self) -> bool:
        return self._connection is not None

    async def acquire(self) -> None:
        """获取锁;已被其他实例持有时抛 :class:`SingleWriterViolation`。"""
        if self._connection is not None:
            raise RuntimeError("single-writer lock is already held by this process")
        connection = await self._engine.connect()
        try:
            acquired = await connection.scalar(
                sa.select(sa.func.pg_try_advisory_lock(self._key))
            )
        except BaseException:
            await connection.close()
            raise
        if acquired is not True:
            await connection.close()
            raise SingleWriterViolation(
                "another Run executor already holds the single-writer lock "
                f"(key={self._key}); 首版执行模型只允许一个 API 进程"
            )
        self._connection = connection

    async def release(self) -> None:
        """释放锁并关闭持有连接;未持有时为空操作。"""
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        await connection.scalar(sa.select(sa.func.pg_advisory_unlock(self._key)))
        await connection.close()
