"""RunJob Repository:PENDING/LEASED 短租约推进(架构文档 §9)。

领取(claim)是全局 Worker 行为,按 §9 领取过程执行 ``FOR UPDATE SKIP LOCKED``,
因此不携带 workspace_id —— 这是工作区过滤规则的唯一例外;完成等其他操作
必须携带 workspace_id 并以 lease token 阻止失租 Worker 提交(fencing)。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import RunJobKind, RunJobStatus
from runtime.persistence.models.execution import RunJob


class JobRepository:
    """run_jobs 表的持久化操作;只 flush 与条件更新,不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        kind: RunJobKind,
        available_at: datetime | None = None,
        max_attempts: int = 3,
    ) -> RunJob:
        """插入 Job;同一 Run 的活动 Job 唯一性由部分唯一索引兜底。"""
        job = RunJob(
            workspace_id=workspace_id,
            run_id=run_id,
            kind=kind,
            max_attempts=max_attempts,
        )
        if available_at is not None:
            job.available_at = available_at
        self._session.add(job)
        await self._session.flush()
        return job

    async def claim_due_job(
        self, *, worker_id: str, lease_duration: timedelta
    ) -> RunJob | None:
        """领取一个到期的 PENDING Job 并置为 LEASED(架构文档 §9 领取过程)。

        ``FOR UPDATE SKIP LOCKED`` 保证多 Worker 并发领取不冲突;
        成功领取时写入 lease token/owner/过期时间并递增 ``attempt_count``。
        """
        stmt = (
            select(RunJob)
            .where(RunJob.status == RunJobStatus.PENDING, RunJob.available_at <= func.now())
            .order_by(RunJob.available_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self._session.scalars(stmt)).one_or_none()
        if job is None:
            return None
        job.status = RunJobStatus.LEASED
        job.lease_owner = worker_id
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = datetime.now(UTC) + lease_duration
        job.attempt_count += 1
        await self._session.flush()
        return job

    async def complete(
        self,
        *,
        workspace_id: uuid.UUID,
        job_id: uuid.UUID,
        lease_token: uuid.UUID,
    ) -> bool:
        """以 lease token 防越权地把 LEASED Job 置为 DONE;返回是否更新到行。"""
        stmt = (
            sa.update(RunJob)
            .where(
                RunJob.id == job_id,
                RunJob.workspace_id == workspace_id,
                RunJob.lease_token == lease_token,
                RunJob.status == RunJobStatus.LEASED,
            )
            .values(status=RunJobStatus.DONE)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return result.rowcount == 1
