"""Worker 进程入口与健康状态测试(计划 04 Task 5;架构文档 §8、§13)。

覆盖启动校验、领取循环、执行中 Job 心跳续租、优雅停止(当前安全点)、
强制超时放弃租约(Sweeper 回收路径)、健康三布尔与无 Job 空转。测试直接
驱动 WorkerHost 夹具,不监听端口、不依赖真实信号。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fixtures.worker_harness import PendingJob, WorkerHostHarness
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from apps.worker.health import HealthReporter, database_probe_from_engine
from apps.worker.main import TransactionalLeaseGateway
from apps.worker.settings import WorkerSettings
from runtime.capabilities.validation import AssemblyError
from runtime.jobs.sweeper import LeaseSweeper
from runtime.persistence.base import (
    RunAttemptStatus,
    RunJobKind,
    RunJobStatus,
    RunStatus,
)
from runtime.persistence.models.execution import Run, RunAttempt, RunJob
from runtime.persistence.repositories.jobs import JobRepository


def long_running_job() -> PendingJob:
    """计划原文长任务:回调循环等待停止请求,把停止请求作为安全点。"""
    return PendingJob(wait_for_stop=True)


def broken_validator() -> None:
    raise AssemblyError("ASSEMBLY_INVALID", "worker test: broken assembly")


async def test_worker_stops_claiming_and_finishes_current_safe_point(
    worker_host: WorkerHostHarness,
) -> None:
    running = await worker_host.start_with_job(long_running_job())
    worker_host.enqueue(PendingJob())  # 停止后不得再领取的新 Job
    await worker_host.request_shutdown()
    assert running.heartbeat_count > 0
    assert worker_host.claimed_after_shutdown == 0
    assert await worker_host.wait_stopped(timeout=5) is True
    # 当前 Job 到达安全点后被正常完成;停止后入队的 Job 未被领取。
    assert worker_host.completed == [running.job.lease.job_id]


async def test_start_rejects_when_assembly_validation_fails(
    worker_host: WorkerHostHarness,
) -> None:
    worker_host.enqueue(PendingJob())
    worker_host.host.validator = broken_validator
    with pytest.raises(AssemblyError):
        await worker_host.host.start()
    # 未进入领取循环:没有任何 claim,也没有 Job 被处理。
    assert worker_host.claim_count == 0
    assert worker_host.completed == []
    report = await worker_host.host.health.report()
    assert report.process_alive is True
    assert report.assembly_valid is False


async def test_health_reports_three_independent_booleans() -> None:
    async def database_down() -> bool:
        return False

    async def database_up() -> bool:
        return True

    async def database_broken() -> bool:
        raise RuntimeError("connection refused")

    reporter = HealthReporter(
        database_probe=database_down, assembly_valid_source=lambda: True
    )
    report = await reporter.report()
    assert report.process_alive is True  # 进程存活独立于数据库故障
    assert report.database_reachable is False
    assert report.assembly_valid is True

    degraded = HealthReporter(
        database_probe=database_broken, assembly_valid_source=lambda: False
    )
    broken = await degraded.report()
    assert broken.process_alive is True
    assert broken.database_reachable is False  # 探针抛异常同样只影响该布尔
    assert broken.assembly_valid is False

    healthy = HealthReporter(
        database_probe=database_up, assembly_valid_source=lambda: True
    )
    good = await healthy.report()
    assert (good.process_alive, good.database_reachable, good.assembly_valid) == (
        True,
        True,
        True,
    )


async def test_database_probe_selects_one(engine: AsyncEngine) -> None:
    assert await database_probe_from_engine(engine)() is True
    missing = create_async_engine(
        "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime_missing_db"
    )
    try:
        assert await database_probe_from_engine(missing)() is False
    finally:
        await missing.dispose()


async def test_running_job_is_renewed_by_heartbeat_until_safe_point(
    worker_host: WorkerHostHarness,
) -> None:
    job = PendingJob(wait_for_heartbeat=True)
    running = await worker_host.start_with_job(job)
    await worker_host.wait_until(lambda: running.heartbeat_count >= 3)
    assert worker_host.completed == []  # 仍在执行,未完成
    job.release.set()  # 到达安全点
    await worker_host.request_shutdown()
    assert await worker_host.wait_stopped(timeout=5) is True
    assert running.heartbeat_count > 0
    assert job.lease.job_id in worker_host.completed
    assert worker_host.released == []


async def test_forced_shutdown_timeout_abandons_lease_for_sweeper(
    worker_host: WorkerHostHarness,
) -> None:
    job = PendingJob()  # 回调阻塞在安全点信号上,无法在超时内到达安全点
    await worker_host.start_with_job(job)
    await worker_host.request_shutdown()
    assert await worker_host.wait_stopped(timeout=5) is True
    # 放弃路径:不 complete 也不 release,租约原样留给 LeaseSweeper 回收。
    assert worker_host.completed == []
    assert worker_host.released == []
    assert job.started.is_set()
    assert not job.safe_point_reached.is_set()


async def test_idle_loop_without_jobs_does_not_error(
    worker_host: WorkerHostHarness,
) -> None:
    await worker_host.host.start()
    await asyncio.sleep(0.05)  # 空转若干轮 claim → None → 轮询
    await worker_host.request_shutdown()
    assert await worker_host.wait_stopped(timeout=5) is True
    assert worker_host.host.last_error is None
    report = await worker_host.host.health.report()
    assert report.process_alive is True
    assert report.assembly_valid is True
    assert worker_host.completed == []


async def test_settings_defaults_and_env_override() -> None:
    defaults = WorkerSettings()
    assert defaults.lease_duration == timedelta(seconds=30)
    assert defaults.validate_assembly is True
    assert defaults.health_port is None

    settings = WorkerSettings.from_env(
        {
            "RUNTIME_DATABASE_URL": "postgresql+asyncpg://x@localhost/db",
            "RUNTIME_WORKER_ID": "worker-9",
            "RUNTIME_LEASE_SECONDS": "5",
            "RUNTIME_HEARTBEAT_SECONDS": "1",
            "RUNTIME_POLL_SECONDS": "0.05",
            "RUNTIME_SHUTDOWN_SECONDS": "2",
            "RUNTIME_VALIDATE_ASSEMBLY": "false",
            "RUNTIME_HEALTH_PORT": "8080",
        }
    )
    assert settings.database_url == "postgresql+asyncpg://x@localhost/db"
    assert settings.worker_id == "worker-9"
    assert settings.lease_duration == timedelta(seconds=5)
    assert settings.heartbeat_interval == timedelta(seconds=1)
    assert settings.poll_interval == timedelta(milliseconds=50)
    assert settings.shutdown_timeout == timedelta(seconds=2)
    assert settings.validate_assembly is False
    assert settings.health_port == 8080


# ---------------------------------------------------------------------------
# 数据库集成:真实 LeaseService 租约网关
# ---------------------------------------------------------------------------


async def _database_job(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    """提交一个 PENDING ADVANCE_RUN Job(Worker 网关使用独立事务)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await JobRepository(session).create(
                workspace_id=workspace_id,
                run_id=run_id,
                kind=RunJobKind.ADVANCE_RUN,
            )
    finally:
        await session.close()


async def _job_status(engine: AsyncEngine, run_id: uuid.UUID) -> RunJobStatus | None:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return await session.scalar(
            sa.select(RunJob.status).where(RunJob.run_id == run_id)
        )
    finally:
        await session.close()


async def _run_status(engine: AsyncEngine, run_id: uuid.UUID) -> RunStatus | None:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return await session.scalar(sa.select(Run.status).where(Run.id == run_id))
    finally:
        await session.close()


async def _lease_expires_at(engine: AsyncEngine, run_id: uuid.UUID) -> datetime | None:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return await session.scalar(
            sa.select(RunJob.lease_expires_at).where(RunJob.run_id == run_id)
        )
    finally:
        await session.close()


async def _latest_attempt(
    engine: AsyncEngine, run_id: uuid.UUID
) -> tuple[RunAttemptStatus, str | None]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        status = await session.scalar(
            sa.select(RunAttempt.status).where(RunAttempt.run_id == run_id)
        )
        code = await session.scalar(
            sa.select(RunAttempt.failure_code).where(RunAttempt.run_id == run_id)
        )
    finally:
        await session.close()
    assert status is not None
    return RunAttemptStatus(status), code


async def test_heartbeat_extends_database_lease(
    harness_factory, worker_seed, engine: AsyncEngine
) -> None:
    harness = harness_factory(
        gateway=TransactionalLeaseGateway(engine),
        heartbeat_interval=timedelta(milliseconds=50),
        poll_interval=timedelta(milliseconds=20),
    )
    await _database_job(engine, worker_seed.workspace_id, worker_seed.run_id)
    await harness.host.start()
    await harness.wait_running()
    initial = await _lease_expires_at(engine, worker_seed.run_id)
    assert initial is not None
    # 心跳持续续租:数据库中的租约过期时间被推迟到超过领取时的值。
    deadline = time.monotonic() + 5
    renewed = initial
    while renewed is not None and renewed <= initial:
        if time.monotonic() > deadline:
            raise AssertionError("database lease was never renewed")
        await asyncio.sleep(0.02)
        renewed = await _lease_expires_at(engine, worker_seed.run_id)
    assert renewed is not None and renewed > initial

    harness.default_release.set()  # 安全点
    await harness.request_shutdown()
    assert await harness.wait_stopped(timeout=5) is True
    assert await _job_status(engine, worker_seed.run_id) is RunJobStatus.DONE


async def test_abandoned_lease_is_recovered_by_lease_sweeper(
    harness_factory, worker_seed, engine: AsyncEngine
) -> None:
    harness = harness_factory(
        gateway=TransactionalLeaseGateway(engine),
        heartbeat_interval=timedelta(milliseconds=50),
        poll_interval=timedelta(milliseconds=20),
        shutdown_timeout=timedelta(milliseconds=150),
    )
    await _database_job(engine, worker_seed.workspace_id, worker_seed.run_id)
    await harness.host.start()
    await harness.wait_running()
    await harness.request_shutdown()  # 当前 Job 无法到达安全点 → 强制超时放弃
    assert await harness.wait_stopped(timeout=5) is True
    # 放弃路径:Job 仍 LEASED(未 complete/未 release),租约原样保留。
    assert await _job_status(engine, worker_seed.run_id) is RunJobStatus.LEASED

    # 回拨租约过期时间,模拟 Sweeper 观察到的失租 Job,然后执行回收。
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            row = (
                await session.scalars(
                    sa.select(RunJob).where(RunJob.run_id == worker_seed.run_id)
                )
            ).one()
            row.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        async with session.begin():
            recovered = await LeaseSweeper(session).sweep_expired()
        assert recovered == 1
    finally:
        await session.close()

    assert await _job_status(engine, worker_seed.run_id) is RunJobStatus.PENDING
    assert await _run_status(engine, worker_seed.run_id) is RunStatus.QUEUED
    attempt_status, attempt_code = await _latest_attempt(engine, worker_seed.run_id)
    assert attempt_status is RunAttemptStatus.ABANDONED
    assert attempt_code == "LEASE_EXPIRED"
