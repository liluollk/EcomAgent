"""RunJob 租约回收器(计划 02 Task 4;架构文档 §13、数据模型文档 §9)。

对租约过期的 LEASED Job(数据模型文档 §9 租约回收器):

- 锁定 Job 与 Run;把该 Run 当前 RUNNING 的最新 Attempt 标记 ``ABANDONED``;
- 写 Operation 处于 ``EXECUTING/UNKNOWN`` 时,下一 Job 必须为
  ``RECONCILE_OPERATION``(同一 Run 只允许一个活动 Job,故原地将过期 Job
  的 ``kind`` 改判为 RECONCILE_OPERATION),Run 由 RUNNING 进入
  ``WAITING_RECONCILIATION``;已是 RECONCILE_OPERATION 的保持 kind 且 Run
  原地等待;
- 否则 Job 回 ``PENDING``(清除旧 token,生成新的可领取周期),
  ``ADVANCE_RUN`` 的 Run 由 RUNNING CAS 回 ``QUEUED``,
  ``RECONCILE_OPERATION`` 的 Run 保持 ``WAITING_RECONCILIATION``。

重试预算耗尽(超过 ``max_attempts`` 进入 DEAD、Run FAILED)属于重试语义,
不在本任务范围。所有写只 flush 不 commit。
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.jobs.lease_service import LeaseError
from runtime.persistence.base import (
    OperationStatus,
    RunAttemptStatus,
    RunJobKind,
    RunJobStatus,
    RunStatus,
)
from runtime.persistence.models.execution import Operation, Run, RunJob, ToolCall
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.runs import RunRepository


_LEASE_EXPIRED = "LEASE_EXPIRED"
"""租约被回收时写入 Attempt 的 failure_code。"""


class LeaseSweeper:
    """回收过期租约;在调用方事务内执行,只 flush 不 commit。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jobs = JobRepository(session)
        self._runs = RunRepository(session)

    async def sweep_expired(self) -> int:
        """回收所有租约过期的 LEASED Job,返回回收数量。"""
        recovered = 0
        for job in await self._jobs.find_expired_leases():
            run = await self._lock_run(workspace_id=job.workspace_id, run_id=job.run_id)
            if run is None:
                continue
            await self._jobs.finish_running_attempt(
                run_id=job.run_id, status=RunAttemptStatus.ABANDONED, failure_code=_LEASE_EXPIRED
            )
            if await self._has_uncertain_operation(
                workspace_id=job.workspace_id, run_id=job.run_id
            ):
                if job.kind is RunJobKind.ADVANCE_RUN:
                    job.kind = RunJobKind.RECONCILE_OPERATION
                if run.status is RunStatus.RUNNING:
                    await self._transition_or_raise(
                        job=job, run=run, target=RunStatus.WAITING_RECONCILIATION
                    )
            elif job.kind is RunJobKind.ADVANCE_RUN and run.status is RunStatus.RUNNING:
                await self._transition_or_raise(job=job, run=run, target=RunStatus.QUEUED)
            # available_at 保持不变:原值早已到期,Job 立即重新可领取;
            # 携带退避的重新入队语义属于 release()(暂时性错误路径)。
            job.status = RunJobStatus.PENDING
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            recovered += 1
        await self._session.flush()
        return recovered

    async def _transition_or_raise(self, *, job: RunJob, run: Run, target: RunStatus) -> None:
        """在已锁定 Run 行的前提下 CAS 推进 Run 状态;失败说明状态被并发修改。"""
        moved = await self._runs.compare_and_set_status(
            workspace_id=job.workspace_id,
            run_id=job.run_id,
            expected_version=run.state_version,
            new_status=target.value,
            expected_status=run.status.value,
        )
        if not moved:
            raise LeaseError(
                f"run {job.run_id} changed concurrently during lease recovery "
                f"(expected {run.status.value})"
            )

    async def _lock_run(self, *, workspace_id: uuid.UUID, run_id: uuid.UUID) -> Run | None:
        """锁定 Run 行(数据模型文档 §9:回收器先锁定 Job 和 Run)。"""
        stmt = (
            select(Run)
            .where(Run.workspace_id == workspace_id, Run.id == run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def _has_uncertain_operation(self, *, workspace_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        """该 Run 是否存在处于 EXECUTING/UNKNOWN 的写 Operation。"""
        stmt = (
            select(Operation.id)
            .join(
                ToolCall,
                sa.and_(
                    ToolCall.workspace_id == Operation.workspace_id,
                    ToolCall.id == Operation.tool_call_id,
                ),
            )
            .where(
                ToolCall.workspace_id == workspace_id,
                ToolCall.run_id == run_id,
                Operation.status.in_((OperationStatus.EXECUTING, OperationStatus.UNKNOWN)),
            )
            .limit(1)
        )
        return (await self._session.scalar(stmt)) is not None
