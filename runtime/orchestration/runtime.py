"""Runtime:Runtime 执行内核的组合根(计划 02 Task 9;ADR-003 执行模型)。

首版执行模型不再有 Job / 租约 / 心跳 / fencing token。一个 Run 的推进路径是:

    RunExecutor.advance_now(run_id)
      → 以 state_version CAS 把 Run 从 QUEUED 推入 RUNNING,创建 RunAttempt
      → Runtime.advance(claim)(回调真实 AgentLoop.advance)
      → 关键节点由 ToolExecutor 自己提交(begin / record_result)

``advance`` 在一个数据库事务内完成本次推进的收尾——Run 状态变更与
AgentEvent 在同一事务提交(架构文档 §9)。循环结果里的 ``next_step`` 由
执行器解释:``RECONCILE`` 触发回查,``ADVANCE`` 把 Run 退回 QUEUED 重排队。

装配说明:backend / assembler / tool_pins / pipeline_factory 由启动装配方
注入;pipeline_factory 每次 advance 调用一次,便于按 Run 构造绑定 Actor 的
真实 PreToolUse 管线。本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.backend.protocol import AgentBackend
from runtime.execution.executor import AdvanceOutcome, RunClaim
from runtime.execution.tool_result_processor import ToolResultProcessor
from runtime.orchestration.agent_loop import (
    AgentLoop,
    ToolCallEvaluator,
    ToolPinResolver,
)
from runtime.orchestration.context import ContextAssembler
from runtime.orchestration.limits import RunLimits
from runtime.persistence.base import RunStatus as PersistenceRunStatus


class Runtime:
    """组合根:把装配好的执行组件接成一个可调用的 Run 推进器。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        backend: AgentBackend,
        assembler: ContextAssembler,
        limits: RunLimits,
        tool_pins: ToolPinResolver,
        pipeline_factory: Callable[[], ToolCallEvaluator],
    ) -> None:
        self._session_factory = session_factory
        self._backend = backend
        self._assembler = assembler
        self._limits = limits
        self._tool_pins = tool_pins
        self._pipeline_factory = pipeline_factory

    async def advance(self, claim: RunClaim) -> AdvanceOutcome:
        """推进一次已由执行器置为 RUNNING 的 Run,提交到下一持久边界。

        不跨外部 I/O 持有长事务:AgentLoop 在模型调用和 Tool 调用前通过
        checkpoint 提交,挂起/终态在返回前再提交一次。
        """
        session = self._session_factory()
        try:
            loop = self._build_loop(session, claim)
            result = await loop.advance(claim.run_id, claim.workspace_id)
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()
        return AdvanceOutcome(
            run_status=PersistenceRunStatus(result.run_status.value),
            failure_code=result.failure_code,
            next_step=result.next_step,
        )

    def _build_loop(self, session: AsyncSession, claim: RunClaim) -> AgentLoop:
        """为该 Run 构造绑定发起者身份的 AgentLoop。"""
        return AgentLoop(
            session,
            workspace_id=claim.workspace_id,
            backend=self._backend,
            assembler=self._assembler,
            limits=self._limits,
            pipeline=self._pipeline_factory(),
            tool_pins=self._tool_pins,
            processor=ToolResultProcessor(),
            checkpoint=session.commit,
        )


__all__ = ["Runtime"]
