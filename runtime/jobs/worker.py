"""RunJob Worker 最小骨架(计划 02 Task 4;架构文档 §8)。

循环 ``claim → 执行回调 → complete``:claim 与 complete 各自使用独立短事务
(领取过程第 6 步:提交事务后才开始模型或 Tool 外部调用),回调在事务外执行。
本任务只做租约生命周期,不实现 AgentLoop;回调返回 :class:`LeaseOutcome`
描述本次推进结果。

异常语义(架构文档 §4.3):回调抛出异常按暂时性错误处理——Job 释放回
PENDING(可重试)并写入 ``WORKER_ERROR`` 错误码,Attempt → FAILED。
:class:`asyncio.CancelledError` 不释放租约,留给 Sweeper 按租约过期回收。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.jobs.lease_service import DEFAULT_LEASE_DURATION, Lease, LeaseOutcome, LeaseService


WORKER_ERROR_CODE = "WORKER_ERROR"
"""Worker 回调异常时写入 Job/Attempt 的错误码(首版不做错误分类)。"""

LeaseHandler = Callable[[Lease], Awaitable[LeaseOutcome | None]]
"""Worker 执行回调:接收租约,返回本次推进结果。"""


class RunJobWorker:
    """领取驱动型 Worker;每次 ``run_once`` 至多处理一个 Job。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        handler: LeaseHandler,
        worker_id: str,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        poll_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        self._session_factory = session_factory
        self._handler = handler
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._poll_interval = poll_interval

    async def run_forever(self) -> None:
        """轮询循环;无可领取 Job 时间歇等待。"""
        while True:
            if not await self.run_once():
                await asyncio.sleep(self._poll_interval.total_seconds())

    async def run_once(self) -> bool:
        """执行一轮 claim → 执行回调 → complete;返回是否处理了 Job。"""
        lease = await self._claim()
        if lease is None:
            return False
        await self._execute(lease)
        return True

    async def _claim(self) -> Lease | None:
        session = self._session_factory()
        try:
            async with session.begin():
                service = LeaseService(session, lease_duration=self._lease_duration)
                return await service.claim(self._worker_id)
        finally:
            await session.close()

    async def _execute(self, lease: Lease) -> None:
        session = self._session_factory()
        try:
            try:
                outcome = await self._handler(lease)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 回调是任意注入代码,失败必须释放租约
                async with session.begin():
                    service = LeaseService(session, lease_duration=self._lease_duration)
                    await service.release(
                        lease.job_id, lease.lease_token, failure_code=WORKER_ERROR_CODE
                    )
                return
            async with session.begin():
                service = LeaseService(session, lease_duration=self._lease_duration)
                await service.complete(lease.job_id, lease.lease_token, outcome)
        finally:
            await session.close()
