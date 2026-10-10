"""Worker 进程宿主(计划 04 Task 5;架构文档 §8、§13)。

生命周期语义:

- ``start``:先执行注入的装配校验器(RuntimeAssembly.validate,架构文档
  §4 启动校验);失败即拒绝启动并抛出原始异常,不进入领取循环;成功后
  启动后台主循环任务。校验开关由组合根按 ``WorkerSettings.validate_assembly``
  决定是否注入校验器(None 表示跳过校验);
- 领取循环:``LeaseGateway.claim`` 全局领取 → 执行注入的 Job 回调
  (Runtime/ProductApplication 推进由组合根注入)→ 按结果 ``complete``;
  回调异常按暂时性错误 ``release``(``WORKER_ERROR``);领取与停止请求竞争
  时,归还刚领取的 Job(``WORKER_SHUTDOWN``);
- 心跳:执行中 Job 由后台任务周期 ``heartbeat(token)`` 续租;心跳失败
  (fencing 失效)即取消回调、放弃提交,租约交由 Sweeper 回收;
- ``request_shutdown``:置停止标志(不再领取新 Job)并等待主循环确认后
  返回;当前 Job 获得 ``shutdown_timeout`` 到达安全点(回调返回),超时则
  取消回调并放弃租约——不 complete 也不 release,由 LeaseSweeper 按租约
  过期回收(架构文档 §8:CancelledError 不释放租约);
- ``wait_stopped``:等待主循环任务结束。

信号处理只在 ``__main__`` 入口注册;测试直接驱动本类,不依赖真实信号。
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from apps.worker.e2e import E2EJobHandler
from apps.worker.health import HealthReporter, database_probe_from_engine
from apps.worker.settings import WorkerSettings
from runtime.jobs.lease_service import (
    DEFAULT_LEASE_DURATION,
    Lease,
    LeaseOutcome,
    LeaseService,
)
from runtime.jobs.worker import WORKER_ERROR_CODE, LeaseHandler


WORKER_SHUTDOWN_CODE = "WORKER_SHUTDOWN"
"""领取与停止请求竞争时归还刚领取 Job 的错误码。"""


class LeaseGateway(Protocol):
    """WorkerHost 依赖的租约边界;生产实现包装 LeaseService 短事务。"""

    async def claim(self, worker_id: str) -> Lease | None: ...

    async def heartbeat(self, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool: ...

    async def complete(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, outcome: LeaseOutcome | None
    ) -> bool: ...

    async def release(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, *, failure_code: str
    ) -> bool: ...


class TransactionalLeaseGateway:
    """把 LeaseService 包进独立短事务的租约网关(生产组合根使用)。

    claim 与心跳/完成各自使用独立短事务(领取过程第 6 步:提交事务后才
    开始模型或 Tool 外部调用)。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> None:
        self._engine = engine
        self._lease_duration = lease_duration

    def _session(self) -> AsyncSession:
        return AsyncSession(self._engine, expire_on_commit=False)

    async def claim(self, worker_id: str) -> Lease | None:
        session = self._session()
        try:
            async with session.begin():
                service = LeaseService(session, lease_duration=self._lease_duration)
                return await service.claim(worker_id)
        finally:
            await session.close()

    async def heartbeat(self, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
        session = self._session()
        try:
            async with session.begin():
                service = LeaseService(session, lease_duration=self._lease_duration)
                return await service.heartbeat(job_id, lease_token)
        finally:
            await session.close()

    async def complete(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, outcome: LeaseOutcome | None
    ) -> bool:
        session = self._session()
        try:
            async with session.begin():
                service = LeaseService(session, lease_duration=self._lease_duration)
                return await service.complete(job_id, lease_token, outcome)
        finally:
            await session.close()

    async def release(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, *, failure_code: str
    ) -> bool:
        session = self._session()
        try:
            async with session.begin():
                service = LeaseService(session, lease_duration=self._lease_duration)
                return await service.release(job_id, lease_token, failure_code=failure_code)
        finally:
            await session.close()


class WorkerHost:
    """领取驱动型 Worker 宿主;由组合根构造,pytest 可直接驱动。"""

    def __init__(
        self,
        *,
        gateway: LeaseGateway,
        handler: LeaseHandler,
        worker_id: str,
        validator: Callable[[], None] | None = None,
        heartbeat_interval: timedelta = timedelta(seconds=10),
        poll_interval: timedelta = timedelta(milliseconds=200),
        shutdown_timeout: timedelta = timedelta(seconds=30),
        health: HealthReporter | None = None,
    ) -> None:
        self._gateway = gateway
        self._handler = handler
        self._worker_id = worker_id
        self.validator = validator
        """启动前可替换的装配校验器;None 表示跳过校验(assembly_valid=True)。"""
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._shutdown_timeout = shutdown_timeout
        self._health = health
        self._stop_requested = asyncio.Event()
        self._stop_observed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.claimed_after_shutdown = 0
        """停止请求之后才被领取(随即归还)的 Job 计数。"""
        self._assembly_valid = False
        self.last_error: BaseException | None = None

    # -- 状态 ---------------------------------------------------------------

    @property
    def shutdown_requested(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def assembly_valid(self) -> bool:
        """启动校验结论;校验通过前为 False,跳过校验视为 True。"""
        return self._assembly_valid

    @property
    def health(self) -> HealthReporter | None:
        return self._health

    # -- 生命周期入口 -------------------------------------------------------

    async def start(self) -> None:
        """执行启动校验并启动主循环;校验失败拒绝启动(不进入领取循环)。"""
        if self._task is not None:
            raise RuntimeError("worker host is already started")
        validator = self.validator
        if validator is not None:
            try:
                validator()
            except Exception as error:
                self._assembly_valid = False
                self.last_error = error
                raise
        self._assembly_valid = True
        self._task = asyncio.create_task(self._run(), name="worker-host")

    async def request_shutdown(self) -> None:
        """请求优雅停止:置停止标志并等待主循环确认;不等待当前安全点。"""
        self._stop_requested.set()
        task = self._task
        if task is None or task.done():
            return
        observer = asyncio.create_task(self._stop_observed.wait())
        try:
            await asyncio.wait({observer, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            observer.cancel()
            with suppress(asyncio.CancelledError):
                await observer

    async def wait_stopped(self, timeout: float) -> bool:
        """等待主循环任务结束;超时返回 False。未启动视为已停止。"""
        task = self._task
        if task is None:
            return True
        done, _ = await asyncio.wait({task}, timeout=timeout)
        return task in done

    # -- 主循环 -------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while not self._stop_requested.is_set():
                lease = await self._gateway.claim(self._worker_id)
                if lease is None:
                    await asyncio.sleep(self._poll_interval.total_seconds())
                    continue
                if self._stop_requested.is_set():
                    # 停止请求在领取期间到达:归还刚领取的 Job,不执行新工作。
                    self.claimed_after_shutdown += 1
                    await self._gateway.release(
                        lease.job_id,
                        lease.lease_token,
                        failure_code=WORKER_SHUTDOWN_CODE,
                    )
                    break
                await self._execute(lease)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — 主循环兜底:记录后置为已停止
            self.last_error = error
        finally:
            self._stop_observed.set()

    async def _invoke_handler(self, lease: Lease) -> LeaseOutcome | None:
        """以协程形式调用注入回调(create_task 需要协程而非任意 Awaitable)。"""
        return await self._handler(lease)

    async def _execute(self, lease: Lease) -> None:
        handler_task = asyncio.create_task(
            self._invoke_handler(lease), name=f"job-{lease.job_id}"
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(lease, handler_task))
        stop_waiter = asyncio.create_task(self._stop_requested.wait())
        try:
            await asyncio.wait(
                {handler_task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_waiter.done() and not handler_task.done():
                # 停止请求先到:限时等待当前安全点(回调返回)。
                self._stop_observed.set()
                await self._await_safe_point_or_abandon(handler_task)
            if handler_task.cancelled():
                # 放弃租约:不 complete 也不 release,由 LeaseSweeper 回收。
                return
            error = handler_task.exception()
            if error is not None:
                await self._gateway.release(
                    lease.job_id, lease.lease_token, failure_code=WORKER_ERROR_CODE
                )
                return
            await self._gateway.complete(
                lease.job_id, lease.lease_token, handler_task.result()
            )
        finally:
            stop_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await stop_waiter
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            if not handler_task.done():
                handler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await handler_task

    async def _await_safe_point_or_abandon(
        self, handler_task: asyncio.Task[LeaseOutcome | None]
    ) -> None:
        """限时等待回调返回;超时取消回调并放弃租约(交由 Sweeper 回收)。"""
        try:
            await asyncio.wait_for(
                asyncio.shield(handler_task),
                timeout=self._shutdown_timeout.total_seconds(),
            )
        except TimeoutError:
            handler_task.cancel()
            with suppress(asyncio.CancelledError):
                await handler_task

    async def _heartbeat_loop(
        self, lease: Lease, handler_task: asyncio.Task[LeaseOutcome | None]
    ) -> None:
        """对执行中 Job 周期续租;失去租约(fencing 失效)即取消回调。"""
        while not handler_task.done():
            await asyncio.sleep(self._heartbeat_interval.total_seconds())
            if handler_task.done():
                return
            renewed = await self._gateway.heartbeat(lease.job_id, lease.lease_token)
            if not renewed:
                handler_task.cancel()
                return


# ---------------------------------------------------------------------------
# 进程入口(python -m apps.worker)
# ---------------------------------------------------------------------------


async def _unhandled_job(lease: Lease) -> LeaseOutcome | None:
    """最小入口的占位回调:无业务推进能力,抛错触发暂时性错误释放。"""
    del lease
    raise RuntimeError("worker entrypoint has no job handler configured")


def _install_signal(
    loop: asyncio.AbstractEventLoop, sig: signal.Signals, trigger: Callable[[], None]
) -> None:
    """优先 loop.add_signal_handler;Windows(Proactor)回退 signal.signal。"""
    try:
        loop.add_signal_handler(sig, trigger)
    except NotImplementedError:
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(trigger))


async def serve(settings: WorkerSettings | None = None) -> int:
    """组装并运行 Worker 进程;收到停止信号后优雅退出,返回退出码。

    ``RUNTIME_E2E=1`` 时使用 E2E 装配开关(:mod:`apps.worker.e2e`):Job
    回调按租约组装 ScriptedBackend + Mock 平台的 ProductApplication 并推进
    已存在的 Run,供端到端验证在无真实模型下走通完整语义。装配校验随
    RuntimeAssembly 组合根接入时注入;HTTP 健康端点不在本任务范围,
    ``health_port`` 预留。
    """
    settings = settings if settings is not None else WorkerSettings.from_env()
    engine = create_async_engine(settings.database_url)
    handler: LeaseHandler = _unhandled_job
    e2e_handler: E2EJobHandler | None = None
    if settings.e2e_mode:
        e2e_handler = E2EJobHandler(engine, worker_id=settings.worker_id)
        handler = e2e_handler
    try:
        state = {"assembly_valid": False}
        host = WorkerHost(
            gateway=TransactionalLeaseGateway(
                engine, lease_duration=settings.lease_duration
            ),
            handler=handler,
            worker_id=settings.worker_id,
            validator=None,
            heartbeat_interval=settings.heartbeat_interval,
            poll_interval=settings.poll_interval,
            shutdown_timeout=settings.shutdown_timeout,
            health=HealthReporter(
                database_probe=database_probe_from_engine(engine),
                assembly_valid_source=lambda: state["assembly_valid"],
            ),
        )
        stop_signal = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            _install_signal(loop, sig, stop_signal.set)
        await host.start()
        state["assembly_valid"] = host.assembly_valid
        signal_task = asyncio.create_task(stop_signal.wait())
        await signal_task
        await host.request_shutdown()
        await host.wait_stopped(timeout=settings.shutdown_timeout.total_seconds() + 5.0)
        return 0
    finally:
        if e2e_handler is not None:
            await e2e_handler.aclose()
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(serve()))
