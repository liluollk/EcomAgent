"""Runtime:Runtime 执行内核的组合根(计划 02 Task 9)。

把既有执行内核服务接成一个真实可调用的 Worker 循环:

    RunJobWorker.run_once()
      → LeaseService.claim(短事务:Run QUEUED→RUNNING、创建 RunAttempt)
      → Runtime.handle_lease(lease)(回调真实 AgentLoop.advance)
      → LeaseService.complete(成功:DONE;确定性失败:DEAD)

- ``handle_lease``:为该租约构造 :class:`AgentLoop`,在一个数据库事务内完成
  本次推进——Run 状态变更与 AgentEvent 在同一事务提交(架构文档 §9);
  Loop 结果的 ``failure_code`` 映射为 Job 的确定性失败(DEAD);
  ``next_job_kind=ADVANCE_RUN``(NOT_APPLIED 重试)映射为释放重排回 PENDING,
  Run 由 RUNNING CAS 回 QUEUED,恢复后重走 PreToolUse;
- ``recover(run_id)``:最小恢复实现——非终态且无活动 Job 的 QUEUED/RUNNING
  Run 重新排队一个 ADVANCE_RUN Job(挂起 Run 的恢复 Job 由 Input/Confirmation
  与 Reconciliation 服务按各自语义创建);
- ``sweep()``::class:`LeaseSweeper` 的调度入口,回收过期租约。

装配说明:backend / assembler / tool_pins / pipeline_factory / handler 由启动
装配方注入(Worker 进程内组件);pipeline_factory 每次 advance 调用一次,
便于按 Run 构造绑定 Actor 的真实 PreToolUse 管线。本模块禁止 import
adapters/agents/eval/apps。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.backend.protocol import AgentBackend
from runtime.execution.operations import ensure_write_fencing
from runtime.execution.tool_result_processor import ToolResultProcessor
from runtime.jobs.lease_service import (
    DEFAULT_LEASE_DURATION,
    Lease,
    LeaseOutcome,
    LeaseService,
)
from runtime.jobs.sweeper import LeaseSweeper
from runtime.jobs.worker import RunJobWorker
from runtime.orchestration.agent_loop import (
    AdvanceResult,
    AgentLoop,
    ToolCallEvaluator,
    ToolPinResolver,
)
from runtime.orchestration.context import ContextAssembler
from runtime.orchestration.limits import RunLimits
from runtime.persistence.base import ACTIVE_JOB_STATUS_VALUES, RunJobKind
from runtime.persistence.models.execution import Run, RunJob
from runtime.persistence.repositories.jobs import JobRepository


TOOL_RETRY_SCHEDULED = "TOOL_RETRY_SCHEDULED"
"""NOT_APPLIED 写操作重新排队时写入 Job 的错误码(临时性重试语义)。"""

_TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class Runtime:
    """组合根:租约驱动的 Agent Loop Worker 与恢复入口。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        backend: AgentBackend,
        assembler: ContextAssembler,
        limits: RunLimits,
        tool_pins: ToolPinResolver,
        pipeline_factory: Callable[[], ToolCallEvaluator],
        worker_id: str = "runtime-worker",
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    ) -> None:
        self._session_factory = session_factory
        self._backend = backend
        self._assembler = assembler
        self._limits = limits
        self._tool_pins = tool_pins
        self._pipeline_factory = pipeline_factory
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._worker = RunJobWorker(
            session_factory=session_factory,
            handler=self.handle_lease,
            worker_id=worker_id,
            lease_duration=lease_duration,
        )

    # -- Worker 循环 ----------------------------------------------------

    async def run_worker_once(self) -> bool:
        """执行一轮 claim → AgentLoop.advance → complete;返回是否处理了 Job。"""
        return await self._worker.run_once()

    async def handle_lease(self, lease: Lease) -> LeaseOutcome:
        """Worker 回调:以短事务检查点推进 Run,不跨外部 I/O 持有事务。"""
        session = self._session_factory()
        try:
            loop = self._build_loop(session, lease)
            result = await loop.advance(
                lease.run_id, lease.workspace_id, lease.lease_token
            )
            await self._checkpoint(session, lease)
            requeue = result.next_job_kind is RunJobKind.ADVANCE_RUN
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
        if not requeue:
            return self._outcome_of(result)

        # NOT_APPLIED 重试:Job 释放回 PENDING,Run 由 RUNNING CAS 回 QUEUED。
        session = self._session_factory()
        try:
            async with session.begin():
                await LeaseService(
                    session, lease_duration=self._lease_duration
                ).release(
                    lease.job_id,
                    lease.lease_token,
                    failure_code=TOOL_RETRY_SCHEDULED,
                )
        finally:
            await session.close()
        return LeaseOutcome(failure_code=None)

    def _outcome_of(self, result: AdvanceResult) -> LeaseOutcome:
        """Loop 结果 → 租约完成语义:确定性失败 → DEAD,其余 → DONE。"""
        if result.failure_code is not None:
            return LeaseOutcome(failure_code=result.failure_code)
        return LeaseOutcome(failure_code=None)

    def _build_loop(self, session: AsyncSession, lease: Lease) -> AgentLoop:
        """为该租约构造绑定 Run 发起者的 AgentLoop。"""
        return AgentLoop(
            session,
            workspace_id=lease.workspace_id,
            backend=self._backend,
            assembler=self._assembler,
            limits=self._limits,
            pipeline=self._pipeline_factory(),
            tool_pins=self._tool_pins,
            processor=ToolResultProcessor(),
            checkpoint=lambda: self._checkpoint(session, lease),
        )

    async def _checkpoint(self, session: AsyncSession, lease: Lease) -> None:
        """校验当前租约仍有效后提交一个完整状态/Event 原子组。"""
        await ensure_write_fencing(
            session,
            workspace_id=lease.workspace_id,
            run_id=lease.run_id,
            lease_token=lease.lease_token,
        )
        await session.commit()

    # -- 恢复与回收 -----------------------------------------------------

    async def recover(
        self, run_id: uuid.UUID, workspace_id: uuid.UUID | None = None
    ) -> bool:
        """最小恢复:非终态且无活动 Job 的 QUEUED/RUNNING Run 重新排队。

        返回是否确保了可领取的 ADVANCE_RUN Job;终态 Run 返回 False。
        """
        session = self._session_factory()
        try:
            async with session.begin():
                run = (
                    await session.scalars(
                        select(Run)
                        .where(Run.id == run_id)
                        .execution_options(populate_existing=True)
                    )
                ).one_or_none()
                if run is None or run.status.value in _TERMINAL_RUN_STATUSES:
                    return False
                if workspace_id is not None and run.workspace_id != workspace_id:
                    return False
                if run.status.value not in ("QUEUED", "RUNNING"):
                    # 挂起 Run 的恢复 Job 由交互/回查服务在其语义内创建。
                    return False
                has_active_job = (
                    await session.scalars(
                        select(RunJob.id).where(
                            RunJob.run_id == run_id,
                            RunJob.status.in_(ACTIVE_JOB_STATUS_VALUES),
                        )
                    )
                ).first()
                if has_active_job is not None:
                    return True
                await JobRepository(session).create(
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    kind=RunJobKind.ADVANCE_RUN,
                )
            return True
        finally:
            await session.close()

    async def sweep(self) -> int:
        """LeaseSweeper 调度入口:回收过期租约,返回回收数量。"""
        session = self._session_factory()
        try:
            async with session.begin():
                return await LeaseSweeper(session).sweep_expired()
        finally:
            await session.close()

    @property
    def worker_id(self) -> str:
        return self._worker_id


__all__ = ["Runtime"]
