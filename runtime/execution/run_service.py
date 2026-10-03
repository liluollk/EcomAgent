"""RunService:状态迁移 + AgentEvent 同事务组合(计划 02 Task 3;架构文档 §4.1、§9)。

组合顺序:``ensure_run_transition`` 静态校验 → ``compare_and_set_status``
CAS 推进(含 workspace_id + state_version,未显式提供 expected_status 时
取读取到的当前状态,防御并发迁移)→ :class:`AgentEventWriter` 追加事件
(序号在同一事务分配)。三者共享同一 AsyncSession。

事务边界:本服务在调用方事务内执行,只 flush 不 commit——状态变更与
AgentEvent 要么一起提交、要么一起回滚(架构文档 §9:Runtime 状态与对应
AgentEvent 在同一数据库事务内提交)。

错误语义:静态非法迁移抛 :class:`InvalidTransition`;CAS 失败
(state_version 或状态已被并发修改)同样抛 :class:`InvalidTransition`,
以 ``reason`` 区分竞争原因;run 不存在或跨 workspace 访问抛
:class:`~runtime.events.writer.RunNotFoundError`。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.events.writer import AgentEventWriter, AppendedEvent, RunNotFoundError
from runtime.execution.transitions import InvalidTransition, ensure_run_transition
from runtime.interfaces.state import RunStatus
from runtime.persistence.repositories.runs import RunRepository


@dataclass(frozen=True)
class TransitionResult:
    """成功迁移的结果:迁移两端状态与同事务写入的事件。"""

    previous_status: RunStatus
    target_status: RunStatus
    event: AppendedEvent


class RunService:
    """封装 ``ensure_run_transition → CAS → AgentEvent`` 的最小组合服务。"""

    def __init__(self, *, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self._workspace_id = workspace_id
        self._runs = RunRepository(session)
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)

    async def transition_run(
        self,
        *,
        run_id: uuid.UUID,
        target_status: RunStatus,
        event_kind: str,
        event_payload: dict[str, Any],
        expected_status: RunStatus | None = None,
        expected_version: int | None = None,
    ) -> TransitionResult:
        """将 Run 迁移到目标状态并在同一事务写入一条 AgentEvent;只 flush 不 commit。

        - 读取当前状态(以读到的 ``state_version`` 作为默认 CAS 期望版本,
          调用方可用 ``expected_version``/``expected_status`` 覆盖);
        - ``ensure_run_transition`` 静态校验,非法抛 :class:`InvalidTransition`;
        - ``compare_and_set_status`` CAS 推进,零行更新(版本过期、状态不匹配
          或越权)抛 :class:`InvalidTransition`,reason 注明竞争原因;
        - 成功后经 :class:`AgentEventWriter` 追加事件(序号在同一事务内分配)。
        """
        run = await self._runs.get(self._workspace_id, run_id)
        if run is None:
            raise RunNotFoundError(f"run {run_id} not found in workspace {self._workspace_id}")
        current = RunStatus(run.status.value)
        version = expected_version if expected_version is not None else run.state_version
        ensure_run_transition(current, target_status)
        expected_status_value = (
            expected_status.value if expected_status is not None else current.value
        )
        moved = await self._runs.compare_and_set_status(
            workspace_id=self._workspace_id,
            run_id=run_id,
            expected_version=version,
            new_status=target_status.value,
            expected_status=expected_status_value,
        )
        if not moved:
            raise InvalidTransition(
                current=current,
                target=target_status,
                reason="concurrent modification: state_version or status no longer matches",
            )
        event = await self._writer.append(run_id, event_kind, event_payload)
        return TransitionResult(previous_status=current, target_status=target_status, event=event)
