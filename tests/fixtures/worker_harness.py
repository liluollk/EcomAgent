"""Worker 生命周期测试夹具(计划 04 Task 5)。

提供 WorkerHost 的测试包装:

- ``FakeLeaseGateway``:内存租约网关 fake,记录每次 claim/heartbeat/complete/release;
- ``PendingJob``:回调行为可注入的测试 Job,安全点由 asyncio.Event 控制;
- ``WorkerHostHarness``:包装 WorkerHost,注入 fake 网关/装配校验/健康探针,
  提供计划测试使用的 ``start_with_job``/``request_shutdown``/
  ``claimed_after_shutdown``/``wait_stopped`` 观测接口。

长任务用 asyncio.Event 控制;测试不监听端口、不依赖真实信号。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from apps.worker.health import HealthReporter
from apps.worker.main import LeaseGateway, WorkerHost
from runtime.jobs.lease_service import Lease, LeaseOutcome


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """轮询等待条件成立;超时抛 AssertionError(测试失败而非悬挂)。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(0.005)


async def _healthy_probe() -> bool:
    return True


class FakeLeaseGateway:
    """内存租约网关 fake:记录 claim/heartbeat/complete/release 供断言。"""

    def __init__(self) -> None:
        self._pending: list[Lease] = []
        self.claim_count = 0
        self._heartbeats: dict[uuid.UUID, int] = {}
        self.completed: list[uuid.UUID] = []
        self.released: list[tuple[uuid.UUID, str]] = []

    def enqueue(self, lease: Lease) -> None:
        self._pending.append(lease)

    def heartbeat_count(self, job_id: uuid.UUID) -> int:
        return self._heartbeats.get(job_id, 0)

    async def claim(self, worker_id: str) -> Lease | None:
        del worker_id
        self.claim_count += 1
        if self._pending:
            return self._pending.pop(0)
        return None

    async def heartbeat(self, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
        del lease_token
        self._heartbeats[job_id] = self._heartbeats.get(job_id, 0) + 1
        return True

    async def complete(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, outcome: LeaseOutcome | None
    ) -> bool:
        del lease_token, outcome
        self.completed.append(job_id)
        return True

    async def release(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, *, failure_code: str
    ) -> bool:
        del lease_token
        self.released.append((job_id, failure_code))
        return True


@dataclass
class PendingJob:
    """测试用待领取 Job;安全点由 asyncio.Event 控制。

    行为模式:
    - 默认:回调阻塞在 ``release`` 事件上(安全点由测试显式给出);
    - ``wait_for_stop=True``:回调循环等待 Worker 停止请求,把停止请求作为
      安全点(计划原文的长任务);
    - ``wait_for_heartbeat=True``:回调先循环观察到首次心跳才继续。
    """

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    safe_point_reached: asyncio.Event = field(default_factory=asyncio.Event)
    wait_for_stop: bool = False
    wait_for_heartbeat: bool = False
    lease: Lease = field(init=False)
    job_id: uuid.UUID = field(init=False)

    def __post_init__(self) -> None:
        self.job_id = uuid.uuid4()
        self.lease = Lease(
            job_id=self.job_id,
            run_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            attempt_id=uuid.uuid4(),
        )


@dataclass
class RunningJob:
    """执行中 Job 的观测视图(计划测试中的 running 对象)。"""

    job: PendingJob
    harness: WorkerHostHarness

    @property
    def heartbeat_count(self) -> int:
        gateway = self.harness.gateway
        assert isinstance(gateway, FakeLeaseGateway)
        return gateway.heartbeat_count(self.job.lease.job_id)


class WorkerHostHarness:
    """包装 WorkerHost:注入 fake 网关、装配校验与健康探针,驱动生命周期断言。"""

    def __init__(
        self,
        *,
        gateway: LeaseGateway | None = None,
        validator: Callable[[], None] | None = None,
        database_probe: Callable[[], Awaitable[bool]] | None = None,
        heartbeat_interval: timedelta = timedelta(milliseconds=10),
        poll_interval: timedelta = timedelta(milliseconds=5),
        shutdown_timeout: timedelta = timedelta(milliseconds=250),
    ) -> None:
        self.gateway: LeaseGateway = gateway if gateway is not None else FakeLeaseGateway()
        self.jobs: dict[uuid.UUID, PendingJob] = {}
        self.default_release = asyncio.Event()
        self.last_lease: Lease | None = None
        self._claims_at_shutdown: int | None = None
        self.host = WorkerHost(
            gateway=self.gateway,
            handler=self._handle_job,
            worker_id="worker-test",
            validator=validator,
            heartbeat_interval=heartbeat_interval,
            poll_interval=poll_interval,
            shutdown_timeout=shutdown_timeout,
            health=HealthReporter(
                database_probe=database_probe
                if database_probe is not None
                else _healthy_probe,
                assembly_valid_source=lambda: self.host.assembly_valid,
            ),
        )

    # -- 观测 ---------------------------------------------------------------

    @property
    def claim_count(self) -> int:
        gateway = self.gateway
        assert isinstance(gateway, FakeLeaseGateway)
        return gateway.claim_count

    @property
    def completed(self) -> list[uuid.UUID]:
        gateway = self.gateway
        assert isinstance(gateway, FakeLeaseGateway)
        return gateway.completed

    @property
    def released(self) -> list[tuple[uuid.UUID, str]]:
        gateway = self.gateway
        assert isinstance(gateway, FakeLeaseGateway)
        return gateway.released

    @property
    def claimed_after_shutdown(self) -> int:
        """request_shutdown 之后的 claim 调用次数(真实网关无计数,恒 0)。"""
        if self._claims_at_shutdown is None or not isinstance(
            self.gateway, FakeLeaseGateway
        ):
            return 0
        return self.gateway.claim_count - self._claims_at_shutdown

    # -- 驱动 ---------------------------------------------------------------

    def enqueue(self, job: PendingJob) -> PendingJob:
        self.jobs[job.lease.job_id] = job
        gateway = self.gateway
        assert isinstance(gateway, FakeLeaseGateway)
        gateway.enqueue(job.lease)
        return job

    async def start_with_job(self, job: PendingJob) -> RunningJob:
        """入队并启动;等待回调开始与首次心跳,返回可观测视图。"""
        self.enqueue(job)
        await self.host.start()
        await wait_until(job.started.is_set)
        gateway = self.gateway
        assert isinstance(gateway, FakeLeaseGateway)
        await wait_until(lambda: gateway.heartbeat_count(job.lease.job_id) > 0)
        return RunningJob(job=job, harness=self)

    async def wait_running(self, *, timeout: float = 5.0) -> Lease:
        """等待任一 Job 进入处理回调(真实网关的数据库集成测试用)。"""
        await wait_until(lambda: self.last_lease is not None, timeout=timeout)
        lease = self.last_lease
        assert lease is not None
        return lease

    async def request_shutdown(self) -> None:
        if self._claims_at_shutdown is None:
            gateway = self.gateway
            self._claims_at_shutdown = (
                gateway.claim_count if isinstance(gateway, FakeLeaseGateway) else 0
            )
        await self.host.request_shutdown()

    async def wait_stopped(self, timeout: float) -> bool:
        return await self.host.wait_stopped(timeout=timeout)

    async def wait_until(self, predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
        await wait_until(predicate, timeout=timeout)

    # -- Job 回调 -----------------------------------------------------------

    async def _handle_job(self, lease: Lease) -> LeaseOutcome | None:
        self.last_lease = lease
        job = self.jobs.get(lease.job_id)
        if job is None:
            # 真实网关领取的数据库 Job:阻塞到测试给出安全点信号。
            await self.default_release.wait()
            return None
        job.started.set()
        if job.wait_for_heartbeat:
            gateway = self.gateway
            assert isinstance(gateway, FakeLeaseGateway)
            await wait_until(
                lambda: gateway.heartbeat_count(lease.job_id) > 0, timeout=5.0
            )
        if job.wait_for_stop:
            # 长任务:循环等待停止请求,把停止请求作为安全点。
            await wait_until(lambda: self.host.shutdown_requested, timeout=10.0)
            job.safe_point_reached.set()
            return None
        await job.release.wait()
        job.safe_point_reached.set()
        return None
