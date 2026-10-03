"""RunJob 租约测试共享夹具(计划 02 Task 4)。

提供:
- ``lease_service``:与 ``db_session`` 同事务的租约服务;
- ``pending_job``:绑定种子 Run 的 PENDING ADVANCE_RUN Job;
- ``expired_lease``:已被领取但租约已过期的 Lease;
- ``sweeper``:与 ``db_session`` 同事务的租约回收器。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.jobs.lease_service import Lease, LeaseService
from runtime.jobs.sweeper import LeaseSweeper
from runtime.persistence.base import RunJobKind
from runtime.persistence.models.execution import Run, RunJob
from runtime.persistence.repositories.jobs import JobRepository


@pytest.fixture()
def lease_service(db_session: AsyncSession) -> LeaseService:
    return LeaseService(db_session)


@pytest.fixture()
def sweeper(db_session: AsyncSession) -> LeaseSweeper:
    return LeaseSweeper(db_session)


@pytest.fixture()
async def pending_job(db_session: AsyncSession, seeded_run: Run) -> RunJob:
    return await JobRepository(db_session).create(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        kind=RunJobKind.ADVANCE_RUN,
    )


@pytest.fixture()
async def expired_lease(
    lease_service: LeaseService, pending_job: RunJob, db_session: AsyncSession
) -> Lease:
    """领取租约后把过期时间回拨到过去,模拟失租 Worker。"""
    lease = await lease_service.claim("worker-a")
    assert lease is not None
    pending_job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    return lease
