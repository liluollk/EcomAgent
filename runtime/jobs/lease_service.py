"""RunJob 租约服务(计划 02 Task 4;架构文档 §8、数据模型文档 §9)。

- ``claim``:``FOR UPDATE SKIP LOCKED`` 领取到期 PENDING Job(全局 Worker
  行为,不带 workspace 过滤),生成 secrets 随机 ``lease_token``、创建
  RunAttempt;``ADVANCE_RUN`` 把 Run 从 QUEUED CAS 到 RUNNING,
  ``RECONCILE_OPERATION`` 要求 Run 已是 WAITING_RECONCILIATION 并保持不变;
- ``heartbeat``:仅当前 token 可续租(fencing);
- ``complete``:仅当前 token 且 Job 仍 LEASED 才成功——成功 Job → DONE、
  Attempt → SUCCEEDED;带 ``failure_code`` 的确定性失败 Job → DEAD、
  Attempt → FAILED;
- ``release``:暂时性错误把 Job 释放回 PENDING(可重试),Attempt → FAILED。

事务边界:所有写只 flush 不 commit,由调用方决定提交/回滚(claim 的 CAS 失败
抛 :class:`LeaseError`,由调用方回滚整个领取事务)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import RunAttemptStatus, RunJobKind, RunJobStatus, RunStatus
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.runs import RunRepository


DEFAULT_LEASE_DURATION = timedelta(seconds=30)
"""领取与心跳的默认租约时长(短租约,见架构文档 §8)。"""


class LeaseError(RuntimeError):
    """租约领取/推进过程中遇到不可恢复的状态竞争。"""


@dataclass(frozen=True)
class Lease:
    """一次成功领取的租约视图;``lease_token`` 是后续操作的 fencing 凭据。"""

    job_id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    lease_token: uuid.UUID
    lease_expires_at: datetime
    attempt_id: uuid.UUID


@dataclass(frozen=True)
class LeaseOutcome:
    """complete 的结果描述;``failure_code`` 为空表示本次推进成功。"""

    failure_code: str | None = None


def _coerce_lease_token(token: str | uuid.UUID) -> uuid.UUID | None:
    """把调用方传入的 token 规整为 UUID;无法解析视为伪造 token(返回 None)。"""
    if isinstance(token, uuid.UUID):
        return token
    try:
        return uuid.UUID(str(token))
    except ValueError:
        return None


class LeaseService:
    """封装 RunJob 租约生命周期;在调用方事务内执行,只 flush 不 commit。"""

    def __init__(
        self, session: AsyncSession, *, lease_duration: timedelta = DEFAULT_LEASE_DURATION
    ) -> None:
        self._session = session
        self._lease_duration = lease_duration
        self._jobs = JobRepository(session)
        self._runs = RunRepository(session)

    async def claim(self, worker_id: str) -> Lease | None:
        """领取一个到期的 PENDING Job;无可领取 Job 返回 None。

        领取成功时生成随机 ``lease_token``、创建 RunAttempt,并按 Job kind
        推进/校验 Run 状态(数据模型文档 §9 领取过程第 5 步)。Run 状态竞争
        抛 :class:`LeaseError`,调用方应回滚领取事务。
        """
        job = await self._jobs.claim_due_job(
            worker_id=worker_id, lease_duration=self._lease_duration
        )
        if job is None:
            return None
        attempt = await self._jobs.create_attempt(job=job, worker_id=worker_id)
        run = await self._runs.get(job.workspace_id, job.run_id)
        if run is None:
            raise LeaseError(f"run {job.run_id} of job {job.id} not found")
        if job.kind is RunJobKind.ADVANCE_RUN:
            moved = await self._runs.compare_and_set_status(
                workspace_id=job.workspace_id,
                run_id=job.run_id,
                expected_version=run.state_version,
                new_status=RunStatus.RUNNING.value,
                expected_status=RunStatus.QUEUED.value,
            )
            if not moved:
                raise LeaseError(
                    f"run {job.run_id} is no longer QUEUED; claim of job {job.id} aborted"
                )
        elif run.status is not RunStatus.WAITING_RECONCILIATION:
            raise LeaseError(
                f"run {job.run_id} must be WAITING_RECONCILIATION for RECONCILE_OPERATION job {job.id}"
            )
        assert job.lease_token is not None
        assert job.lease_expires_at is not None
        return Lease(
            job_id=job.id,
            run_id=job.run_id,
            workspace_id=job.workspace_id,
            lease_token=job.lease_token,
            lease_expires_at=job.lease_expires_at,
            attempt_id=attempt.id,
        )

    async def heartbeat(
        self,
        job_id: uuid.UUID,
        lease_token: str | uuid.UUID,
        *,
        lease_duration: timedelta | None = None,
    ) -> bool:
        """心跳续租:仅当前 token 且 Job 仍 LEASED 才成功;过期时间后移。"""
        token = _coerce_lease_token(lease_token)
        if token is None:
            return False
        return await self._jobs.heartbeat(
            job_id=job_id,
            lease_token=token,
            lease_duration=lease_duration if lease_duration is not None else self._lease_duration,
        )

    async def complete(
        self,
        job_id: uuid.UUID,
        lease_token: str | uuid.UUID,
        outcome: LeaseOutcome | None = None,
    ) -> bool:
        """以 fencing token 完成租约;token 过期或 Job 不再 LEASED 返回 False。

        默认 outcome 为成功:Job → DONE、Attempt → SUCCEEDED;提供
        ``LeaseOutcome(failure_code=...)`` 视为确定性失败:Job → DEAD、
        Attempt → FAILED 并记录错误码。
        """
        token = _coerce_lease_token(lease_token)
        if token is None:
            return False
        job = await self._jobs.get(job_id)
        if job is None:
            return False
        failure_code = outcome.failure_code if outcome is not None else None
        if failure_code is None:
            committed = await self._jobs.complete(
                workspace_id=job.workspace_id, job_id=job_id, lease_token=token
            )
            attempt_status, attempt_code = RunAttemptStatus.SUCCEEDED, None
        else:
            committed = await self._jobs.fail_lease(
                job_id=job_id,
                lease_token=token,
                target_status=RunJobStatus.DEAD,
                error_code=failure_code,
            )
            attempt_status, attempt_code = RunAttemptStatus.FAILED, failure_code
        if not committed:
            return False
        return await self._jobs.finish_running_attempt(
            run_id=job.run_id, status=attempt_status, failure_code=attempt_code
        )

    async def release(
        self,
        job_id: uuid.UUID,
        lease_token: str | uuid.UUID,
        *,
        failure_code: str,
        retry_delay: timedelta | None = None,
    ) -> bool:
        """暂时性错误:释放租约使 Job 回 PENDING(可重试),Attempt → FAILED。

        ``ADVANCE_RUN`` 的 Run 同时由 RUNNING CAS 回 QUEUED,使下一 Worker 可
        重新领取(数据模型文档 §9);提供 ``retry_delay`` 时按退避改写
        ``available_at``,否则立即重新可领取。
        """
        token = _coerce_lease_token(lease_token)
        if token is None:
            return False
        job = await self._jobs.get(job_id)
        if job is None:
            return False
        available_at = (
            datetime.now(UTC) + retry_delay if retry_delay is not None else None
        )
        committed = await self._jobs.fail_lease(
            job_id=job_id,
            lease_token=token,
            target_status=RunJobStatus.PENDING,
            error_code=failure_code,
            available_at=available_at,
        )
        if not committed:
            return False
        if job.kind is RunJobKind.ADVANCE_RUN:
            # 数据模型文档 §9:ADVANCE_RUN 释放重试时 Run 由 RUNNING 回 QUEUED,
            # 使下一 Worker 可重新完成 QUEUED→RUNNING CAS;RECONCILE_OPERATION
            # 的 Run 保持 WAITING_RECONCILIATION。
            run = await self._runs.get(job.workspace_id, job.run_id)
            if run is None:
                raise LeaseError(f"run {job.run_id} of job {job.id} not found")
            if run.status is RunStatus.RUNNING:
                moved = await self._runs.compare_and_set_status(
                    workspace_id=job.workspace_id,
                    run_id=job.run_id,
                    expected_version=run.state_version,
                    new_status=RunStatus.QUEUED.value,
                    expected_status=RunStatus.RUNNING.value,
                )
                if not moved:
                    raise LeaseError(
                        f"run {job.run_id} changed concurrently; release of job {job.id} aborted"
                    )
        return await self._jobs.finish_running_attempt(
            run_id=job.run_id, status=RunAttemptStatus.FAILED, failure_code=failure_code
        )
