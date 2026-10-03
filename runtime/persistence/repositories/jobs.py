"""RunJob Repository:PENDING/LEASED 短租约推进(架构文档 §9)。

领取(claim)是全局 Worker 行为,按 §9 领取过程执行 ``FOR UPDATE SKIP LOCKED``,
因此不携带 workspace_id —— 这是工作区过滤规则的唯一例外;完成等其他操作
必须携带 workspace_id 并以 lease token 阻止失租 Worker 提交(fencing)。
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import RunAttemptStatus, RunJobKind, RunJobStatus
from runtime.persistence.models.execution import RunAttempt, RunJob


def _new_lease_token() -> uuid.UUID:
    """secrets CSPRNG 生成的 UUIDv4 形态 lease token(架构文档 §8 随机 token)。"""
    return uuid.UUID(bytes=secrets.token_bytes(16), version=4)


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
        job.lease_token = _new_lease_token()
        job.lease_expires_at = datetime.now(UTC) + lease_duration
        job.attempt_count += 1
        await self._session.flush()
        return job

    async def get(self, job_id: uuid.UUID) -> RunJob | None:
        """按主键读取 Job(供租约服务确定 fencing 目标与 workspace 归属)。"""
        return await self._session.get(RunJob, job_id)

    async def heartbeat(
        self, *, job_id: uuid.UUID, lease_token: uuid.UUID, lease_duration: timedelta
    ) -> bool:
        """以 lease token 防越权地把 LEASED Job 的租约延长 ``lease_duration``。"""
        stmt = (
            sa.update(RunJob)
            .where(
                RunJob.id == job_id,
                RunJob.lease_token == lease_token,
                RunJob.status == RunJobStatus.LEASED,
            )
            .values(lease_expires_at=func.now() + lease_duration)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return result.rowcount == 1

    async def fail_lease(
        self,
        *,
        job_id: uuid.UUID,
        lease_token: uuid.UUID,
        target_status: RunJobStatus,
        error_code: str,
        available_at: datetime | None = None,
    ) -> bool:
        """以 lease token 防越权地结束一次失败的租约。

        ``target_status`` 为 ``PENDING``(暂时性错误,释放重试)或 ``DEAD``
        (确定性错误);清除租约字段并写入分类后的错误码。提供 ``available_at``
        时同时改写可领取时间(退避)。
        """
        values: dict[str, Any] = {
            "status": target_status,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "last_error_code": error_code,
        }
        if available_at is not None:
            values["available_at"] = available_at
        stmt = (
            sa.update(RunJob)
            .where(
                RunJob.id == job_id,
                RunJob.lease_token == lease_token,
                RunJob.status == RunJobStatus.LEASED,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return result.rowcount == 1

    async def find_expired_leases(self) -> list[RunJob]:
        """锁定所有租约过期的 LEASED Job;``SKIP LOCKED`` 允许多 Sweeper 并行。"""
        stmt = (
            select(RunJob)
            .where(RunJob.status == RunJobStatus.LEASED, RunJob.lease_expires_at <= func.now())
            .order_by(RunJob.lease_expires_at)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.scalars(stmt)).all())

    async def create_attempt(self, *, job: RunJob, worker_id: str) -> RunAttempt:
        """为领取到的 Job 创建新 RunAttempt,``attempt_no`` 按 Run 内递增。"""
        stmt = select(func.max(RunAttempt.attempt_no)).where(RunAttempt.run_id == job.run_id)
        highest = await self._session.scalar(stmt)
        attempt = RunAttempt(
            workspace_id=job.workspace_id,
            run_id=job.run_id,
            attempt_no=int(highest or 0) + 1,
            worker_id=worker_id,
            status=RunAttemptStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def finish_running_attempt(
        self,
        *,
        run_id: uuid.UUID,
        status: RunAttemptStatus,
        failure_code: str | None = None,
    ) -> bool:
        """收尾该 Run 当前 ``RUNNING`` 的最新 Attempt;无匹配行返回 False。"""
        latest_running = (
            select(RunAttempt.id)
            .where(RunAttempt.run_id == run_id, RunAttempt.status == RunAttemptStatus.RUNNING)
            .order_by(RunAttempt.attempt_no.desc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            sa.update(RunAttempt)
            .where(RunAttempt.id == latest_running)
            .values(status=status, finished_at=func.now(), failure_code=failure_code)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return result.rowcount == 1

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
