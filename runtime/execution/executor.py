"""进程内 Run 执行器(首版执行模型,ADR-003)。

首版明确限定「单 API 进程、单 Runtime Executor」,因此不再需要可被多实例
抢占的 RunJob、租约、心跳与 fencing token。执行模型退化为:

    API 创建 Run(QUEUED)
      → RunExecutor 以 state_version CAS 把 Run 推入 RUNNING
      → RunDriver.advance 在进程内推进到下一个持久边界
      → 关键节点由 ToolExecutor 自行提交(begin / record_result 各一次)
      → WAITING_INPUT / WAITING_CONFIRMATION 时自然停下,等用户响应
      → 进程重启时 recover() 扫描非终态 Run:先回查不确定的外部写,再继续推进

并发控制只剩两层,都是必须保留的:

1. ``Run.state_version`` CAS——防止同一 Run 被两个推进者同时写;
2. 进程级单写者锁(:mod:`runtime.execution.single_writer`)——防止同一库上
   出现第二个执行器。它只在进程启动时竞争一次,不参与任务调度。

本模块不感知领域语义:``RunDriver`` 由组合根注入,负责"怎么推进"。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.execution.single_writer import SingleWriterLock
from runtime.persistence.base import (
    OperationStatus,
    RunAttemptStatus,
    RunNextStep,
    RunStatus,
)
from runtime.persistence.models.execution import Operation, Run, RunAttempt, ToolCall
from runtime.persistence.repositories.runs import RunRepository


TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
"""终态 Run;恢复扫描不触碰。"""

RECOVERABLE_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_RECONCILIATION}
)
"""启动恢复扫描的入口集合。

``RUNNING`` 在启动时一律解读为"上次进程中断",而不是"正在执行"——进程内
只有本执行器能推进 Run,新进程启动时不可能存在并发推进者。``WAITING_INPUT``
与 ``WAITING_CONFIRMATION`` 不在集合内:它们等待的是用户输入,不是执行能力。
"""

DEFAULT_MAX_ATTEMPTS = 3
"""同一 Run 连续推进失败的预算;耗尽后 Run 落 FAILED,不再自动重试。"""

DEFAULT_IDLE_POLL_SECONDS = 1.0
"""无待办 Run 时的兜底扫描间隔。"""

FAILURE_BACKOFF_CEILING_SECONDS = 30.0
"""后台循环连续失败时的退避上限;避免数据库不可用期间高频重试。"""


@dataclass(frozen=True)
class RunClaim:
    """一次执行器会话对某个 Run 的推进凭据(不含租约语义)。"""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    membership_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int


@dataclass(frozen=True)
class AdvanceOutcome:
    """一次推进的结果;``next_step`` 是执行器内部的调度指令。"""

    run_status: RunStatus
    failure_code: str | None = None
    next_step: RunNextStep | None = None


@dataclass
class RecoveryReport:
    """启动恢复扫描的观测结果。"""

    advanced: list[uuid.UUID] = field(default_factory=list)
    reconciled: list[uuid.UUID] = field(default_factory=list)
    skipped: list[uuid.UUID] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.advanced) + len(self.reconciled) + len(self.skipped)


class RunDriver(Protocol):
    """组合根注入的推进实现;执行器只负责调度与状态前置校验。"""

    async def advance(
        self, claim: RunClaim, *, answered: Mapping[str, str] | None = None
    ) -> AdvanceOutcome: ...

    async def reconcile(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool: ...


class RunExecutor:
    """单进程内的 Run 推进器。

    - ``submit``:非阻塞唤醒,让后台循环尽快处理某个 Run(API 提交消息后调用);
    - ``advance_now``:同步推进一次,供组合根/E2E 在请求内等待到下一个边界;
    - ``recover``:启动时扫描并接管非终态 Run;
    - ``start``/``aclose``:获取单写者锁、启动兜底循环、优雅退出。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        driver: RunDriver,
        instance_id: str,
        lock: SingleWriterLock | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        idle_poll_interval: float = DEFAULT_IDLE_POLL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._driver = driver
        self._instance_id = instance_id
        self._lock = lock
        self._max_attempts = max_attempts
        self._idle_poll_interval = idle_poll_interval
        self._pending: list[uuid.UUID] = []
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error: BaseException | None = None
        self._consecutive_failures = 0

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def consecutive_failures(self) -> int:
        """后台循环连续失败的轮数;成功推进一轮即归零。"""
        return self._consecutive_failures

    @property
    def healthy(self) -> bool:
        """循环仍在运行即视为可推进;失败次数由调用方决定告警阈值。"""
        return self.running

    # -- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        """获取单写者锁 → 恢复扫描 → 启动兜底循环。

        锁失败即抛 :class:`runtime.execution.single_writer.SingleWriterViolation`,
        调用方应拒绝启动(而不是降级为只读)。
        """
        if self._task is not None:
            raise RuntimeError("run executor is already started")
        if self._lock is not None:
            await self._lock.acquire()
        try:
            await self.recover()
        except BaseException:
            if self._lock is not None:
                await self._lock.release()
            raise
        self._task = asyncio.create_task(self._loop(), name="run-executor")

    async def aclose(self) -> None:
        """停止领取新 Run,等待当前推进到安全点后释放锁。"""
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # noqa: BLE001 — 关闭路径不能因残留异常失败
                self.last_error = error
        if self._lock is not None:
            await self._lock.release()

    # -- 提交与推进 -----------------------------------------------------

    def submit(self, run_id: uuid.UUID) -> None:
        """非阻塞入队;同一 Run 重复提交由 CAS 去重。"""
        self._pending.append(run_id)
        self._wake.set()

    async def advance_now(self, run_id: uuid.UUID) -> AdvanceOutcome | None:
        """同步推进一个 Run;不可推进(非 QUEUED/终态/已被接管)返回 None。"""
        claim = await self._claim(run_id)
        if claim is None:
            return None
        return await self._execute(claim)

    async def reconcile_now(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        """委托 driver 回查该 Run 的 UNKNOWN Operation。"""
        return await self._driver.reconcile(run_id, workspace_id)

    # -- 恢复扫描 -------------------------------------------------------

    async def recover(self) -> RecoveryReport:
        """接管非终态 Run:先处理不确定的外部写,再继续推进。

        顺序不可交换。若先推进再有 UNKNOWN Operation 的 Run,Agent Loop 会把
        "可能已生效的写"当成未发生,从而重复写外部系统。
        """
        report = RecoveryReport()
        candidates = await self._recoverable_runs()
        pending_reconcile: list[tuple[uuid.UUID, uuid.UUID]] = []
        pending_advance: list[uuid.UUID] = []
        for run, has_uncertain in candidates:
            if run.status is RunStatus.RUNNING:
                # 启动时仍 RUNNING 的 Attempt 必然是上次进程崩溃遗留:先收尾,
                # 否则审计视图会永久停在"正在执行"。这一步承接了原 Sweeper 的
                # 第二项职责,只是触发条件从"租约过期"变成"进程重启"。
                await self._abandon_open_attempts(run)
            if run.status is RunStatus.WAITING_RECONCILIATION or has_uncertain:
                if run.status is RunStatus.RUNNING:
                    moved = await self._cas_status(
                        run, target=RunStatus.WAITING_RECONCILIATION
                    )
                    if not moved:
                        report.skipped.append(run.id)
                        continue
                pending_reconcile.append((run.id, run.workspace_id))
            elif run.status is RunStatus.RUNNING:
                # 启动时的 RUNNING 一律视为上次中断;退回 QUEUED 以便重新推进。
                if await self._cas_status(run, target=RunStatus.QUEUED):
                    pending_advance.append(run.id)
                else:
                    report.skipped.append(run.id)
            else:
                pending_advance.append(run.id)

        for run_id, workspace_id in pending_reconcile:
            if await self.reconcile_now(run_id, workspace_id):
                report.reconciled.append(run_id)
                # 回查消除不确定性后继续推进同一 Run(仍是 QUEUED)。
                if await self.advance_now(run_id) is not None:
                    report.advanced.append(run_id)
            else:
                report.skipped.append(run_id)
        for run_id in pending_advance:
            if await self.advance_now(run_id) is not None:
                report.advanced.append(run_id)
            else:
                report.skipped.append(run_id)
        return report

    async def _recoverable_runs(self) -> list[tuple[Run, bool]]:
        """读取待恢复 Run 及其是否存在不确定 Operation。"""
        session = self._session_factory()
        try:
            runs = list(
                await session.scalars(
                    select(Run)
                    .where(Run.status.in_([status.value for status in RECOVERABLE_RUN_STATUSES]))
                    .order_by(Run.created_at)
                    .execution_options(populate_existing=True)
                )
            )
            result: list[tuple[Run, bool]] = []
            for run in runs:
                result.append((run, await self._has_uncertain_operation(session, run)))
            return result
        finally:
            await session.close()

    async def _has_uncertain_operation(self, session: AsyncSession, run: Run) -> bool:
        """该 Run 是否有处于 EXECUTING/UNKNOWN 的写 Operation。"""
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
                ToolCall.workspace_id == run.workspace_id,
                ToolCall.run_id == run.id,
                Operation.status.in_((OperationStatus.EXECUTING, OperationStatus.UNKNOWN)),
            )
            .limit(1)
        )
        return (await session.scalar(stmt)) is not None

    async def _abandon_open_attempts(self, run: Run) -> None:
        """把崩溃遗留的 RUNNING Attempt 标记为 ABANDONED(仅用于事后归因)。"""
        session = self._session_factory()
        try:
            async with session.begin():
                await session.execute(
                    sa.update(RunAttempt)
                    .where(
                        RunAttempt.workspace_id == run.workspace_id,
                        RunAttempt.run_id == run.id,
                        RunAttempt.status == RunAttemptStatus.RUNNING,
                    )
                    .values(
                        status=RunAttemptStatus.ABANDONED,
                        failure_code="EXECUTOR_RESTART",
                        finished_at=datetime.now(UTC),
                    )
                    .execution_options(synchronize_session=False)
                )
        finally:
            await session.close()

    async def _cas_status(self, run: Run, *, target: RunStatus) -> bool:
        session = self._session_factory()
        try:
            async with session.begin():
                return await RunRepository(session).compare_and_set_status(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    expected_version=run.state_version,
                    new_status=target.value,
                    expected_status=run.status.value,
                )
        finally:
            await session.close()

    # -- 内部:领取与执行 -------------------------------------------------

    async def _claim(self, run_id: uuid.UUID) -> RunClaim | None:
        """CAS QUEUED → RUNNING 并创建 RunAttempt;竞争失败返回 None。"""
        session = self._session_factory()
        try:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(Run)
                        .where(Run.id == run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).one_or_none()
                if run is None or run.status is not RunStatus.QUEUED:
                    return None
                moved = await RunRepository(session).compare_and_set_status(
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    expected_version=run.state_version,
                    new_status=RunStatus.RUNNING.value,
                    expected_status=RunStatus.QUEUED.value,
                )
                if not moved:
                    return None
                attempt_no = (
                    await session.scalar(
                        select(func.max(RunAttempt.attempt_no)).where(
                            RunAttempt.run_id == run_id
                        )
                    )
                    or 0
                ) + 1
                attempt = RunAttempt(
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    attempt_no=attempt_no,
                    executor_id=self._instance_id,
                    status=RunAttemptStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
                session.add(attempt)
                await session.flush()
                return RunClaim(
                    run_id=run_id,
                    workspace_id=run.workspace_id,
                    actor_id=run.created_by_actor_id,
                    membership_id=run.created_by_membership_id,
                    attempt_id=attempt.id,
                    attempt_no=attempt_no,
                )
        finally:
            await session.close()

    async def _execute(self, claim: RunClaim) -> AdvanceOutcome:
        """推进一次并把结果落到 Attempt / Run 状态。"""
        try:
            outcome = await self._driver.advance(claim)
        except asyncio.CancelledError:
            await self._finish_attempt(claim, RunAttemptStatus.ABANDONED, "EXECUTOR_SHUTDOWN")
            raise
        except Exception as error:  # noqa: BLE001 — 任意 driver 异常都必须收尾
            self.last_error = error
            return await self._handle_failure(claim, error)

        await self._finish_attempt(
            claim,
            RunAttemptStatus.SUCCEEDED if outcome.failure_code is None else RunAttemptStatus.FAILED,
            outcome.failure_code,
        )
        if outcome.next_step is RunNextStep.RECONCILE:
            # 回查消除不确定性后 Run 回到 QUEUED;立即入队续推(与恢复扫描
            # 同一语义),不依赖兜底轮询的下一次扫描。
            if await self.reconcile_now(claim.run_id, claim.workspace_id):
                self.submit(claim.run_id)
        elif outcome.next_step is RunNextStep.ADVANCE:
            # NOT_APPLIED 重试:Run 仍是 RUNNING,退回 QUEUED 交给下一轮推进。
            await self._requeue(claim)
        return outcome

    async def _handle_failure(self, claim: RunClaim, error: BaseException) -> AdvanceOutcome:
        """暂时性错误:未耗尽预算退回 QUEUED,否则 Run FAILED。

        ``retryable = False`` 的异常是永久性配置/装配错误(如 Workspace 未配置
        模型):重试同一份配置不会变好,因此直接判失败,不占用重试预算。
        """
        code = str(getattr(error, "code", "") or type(error).__name__).upper()[:64]
        permanent = getattr(error, "retryable", True) is False
        await self._finish_attempt(claim, RunAttemptStatus.FAILED, code)
        session = self._session_factory()
        try:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(Run)
                        .where(Run.id == claim.run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).one_or_none()
                if run is None or run.status is not RunStatus.RUNNING:
                    # 推进事务可能已提交到 WAITING_* 等边界,失败不再改写状态。
                    return AdvanceOutcome(
                        run_status=run.status if run is not None else RunStatus.FAILED,
                        failure_code=code,
                    )
                target = (
                    RunStatus.FAILED
                    if permanent or claim.attempt_no >= self._max_attempts
                    else RunStatus.QUEUED
                )
                await RunRepository(session).compare_and_set_status(
                    workspace_id=run.workspace_id,
                    run_id=claim.run_id,
                    expected_version=run.state_version,
                    new_status=target.value,
                    expected_status=RunStatus.RUNNING.value,
                )
                if target is RunStatus.FAILED:
                    # 失败码写进 Run 行:只留在 Attempt 里的话,读 Run 的人
                    # (API、评测、排障)只能看到 status=FAILED 而不知道原因。
                    await session.execute(
                        sa.update(Run)
                        .where(
                            Run.workspace_id == run.workspace_id,
                            Run.id == claim.run_id,
                        )
                        .values(failure_code=code)
                        .execution_options(synchronize_session=False)
                    )
        finally:
            await session.close()
        if target is RunStatus.QUEUED:
            self.submit(claim.run_id)
        return AdvanceOutcome(run_status=target, failure_code=code)

    async def _requeue(self, claim: RunClaim) -> None:
        session = self._session_factory()
        try:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(Run)
                        .where(Run.id == claim.run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).one_or_none()
                if run is None or run.status is not RunStatus.RUNNING:
                    return
                await RunRepository(session).compare_and_set_status(
                    workspace_id=run.workspace_id,
                    run_id=claim.run_id,
                    expected_version=run.state_version,
                    new_status=RunStatus.QUEUED.value,
                    expected_status=RunStatus.RUNNING.value,
                )
        finally:
            await session.close()
        self.submit(claim.run_id)

    async def _finish_attempt(
        self,
        claim: RunClaim,
        status: RunAttemptStatus,
        failure_code: str | None,
    ) -> None:
        session = self._session_factory()
        try:
            async with session.begin():
                await session.execute(
                    sa.update(RunAttempt)
                    .where(
                        RunAttempt.workspace_id == claim.workspace_id,
                        RunAttempt.id == claim.attempt_id,
                    )
                    .values(
                        status=status,
                        failure_code=failure_code,
                        finished_at=datetime.now(UTC),
                    )
                    .execution_options(synchronize_session=False)
                )
        finally:
            await session.close()

    # -- 后台循环 -------------------------------------------------------

    async def _loop(self) -> None:
        """兜底循环:消费内存队列,队列为空时扫库找待处理的 Run。

        数据库是唯一队列,内存列表只是"尽快处理"的提示。因此 ``submit``
        丢失(进程在推进前崩溃)不会丢任务——下次启动的恢复扫描会捡回来。

        **整轮**都包在异常边界内,包括队列探测:探测失败只让本轮失败,
        不能让后台任务退出。此前 ``_next_queued`` 在 try 之外,一次连接抖动
        就会终结整个执行器,而所有 Run 仍停在 QUEUED、健康检查仍为绿灯。
        连续失败按指数退避,避免数据库不可用时把日志和连接池打满。

        扫描同时覆盖 ``WAITING_RECONCILIATION``:此前只有启动恢复会回查这类
        Run,进程活着时一次回查失败就会让它在等待态永久滞留。
        """
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                target = await self._next_actionable()
                if target is None:
                    await self._idle_wait()
                    continue
                run_id, workspace_id, status = target
                if status is RunStatus.WAITING_RECONCILIATION:
                    await self._reconcile_pending(run_id, workspace_id)
                else:
                    await self.advance_now(run_id)
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — 单轮失败不能让循环退出
                self.last_error = error
                consecutive_failures += 1
                self._consecutive_failures = consecutive_failures
                await self._failure_backoff(consecutive_failures)

    async def _next_actionable(self) -> tuple[uuid.UUID, uuid.UUID, RunStatus] | None:
        """下一项待处理工作:内存提示优先,其次等待回查,最后 QUEUED。

        等待回查的 Run 排在 QUEUED 之前:它占着一次外部写的不确定性,继续
        推迟只会让"可能已生效"的时间更长;数量受回查预算约束,不会长期挤压。
        """
        if self._pending:
            run_id = self._pending.pop(0)
            current = await self._run_state(run_id)
            if current is not None:
                workspace_id, status = current
                return run_id, workspace_id, status
        return await self._scan_actionable()

    async def _run_state(self, run_id: uuid.UUID) -> tuple[uuid.UUID, RunStatus] | None:
        session = self._session_factory()
        try:
            row = (
                await session.execute(
                    select(Run.workspace_id, Run.status).where(Run.id == run_id)
                )
            ).first()
            return (row[0], RunStatus(row[1])) if row is not None else None
        finally:
            await session.close()

    async def _scan_actionable(self) -> tuple[uuid.UUID, uuid.UUID, RunStatus] | None:
        """按状态优先级扫库:等待回查在前,QUEUED 在后。"""
        session = self._session_factory()
        try:
            for status in (RunStatus.WAITING_RECONCILIATION, RunStatus.QUEUED):
                row = (
                    await session.execute(
                        select(Run.id, Run.workspace_id)
                        .where(Run.status == status.value)
                        .order_by(Run.updated_at)
                        .limit(1)
                    )
                ).first()
                if row is not None:
                    return row[0], row[1], status
            return None
        finally:
            await session.close()

    async def _reconcile_pending(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        """回查一个等待中的 Run;回查本身失败时把 Run 判失败,不留悬挂。

        数据不一致(没有或有多条不确定 Operation)会让回查永远返回 False,
        若只是跳过,这个 Run 会永久停在 WAITING_RECONCILIATION 且不再被任何
        路径处理。这里把它收敛到 FAILED,错误码保留原因。
        """
        try:
            resolved = await self.reconcile_now(run_id, workspace_id)
        except Exception as error:  # noqa: BLE001 — 回查失败必须收敛为终态
            self.last_error = error
            code = str(getattr(error, "code", "") or type(error).__name__).upper()[:64]
            await self._fail_run(
                run_id=run_id,
                expected_status=RunStatus.WAITING_RECONCILIATION,
                code=code,
            )
            return False
        if resolved:
            await self.advance_now(run_id)
        return resolved

    async def _fail_run(
        self, *, run_id: uuid.UUID, expected_status: RunStatus, code: str
    ) -> bool:
        """把指定状态的 Run 以 CAS 推入 FAILED 并记录失败码。"""
        session = self._session_factory()
        try:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(Run)
                        .where(Run.id == run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).one_or_none()
                if run is None or run.status is not expected_status:
                    return False
                moved = await RunRepository(session).compare_and_set_status(
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    expected_version=run.state_version,
                    new_status=RunStatus.FAILED.value,
                    expected_status=expected_status.value,
                )
                if not moved:
                    return False
                await session.execute(
                    sa.update(Run)
                    .where(Run.workspace_id == run.workspace_id, Run.id == run_id)
                    .values(failure_code=code)
                    .execution_options(synchronize_session=False)
                )
                return True
        finally:
            await session.close()

    async def _idle_wait(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._idle_poll_interval)
        except TimeoutError:
            pass

    async def _failure_backoff(self, failures: int) -> None:
        """连续失败后的退避;有 ``submit`` 唤醒时提前结束。"""
        delay = min(
            self._idle_poll_interval * (2 ** min(failures - 1, 5)),
            FAILURE_BACKOFF_CEILING_SECONDS,
        )
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except TimeoutError:
            pass


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "RECOVERABLE_RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "AdvanceOutcome",
    "RecoveryReport",
    "RunClaim",
    "RunDriver",
    "RunExecutor",
]
