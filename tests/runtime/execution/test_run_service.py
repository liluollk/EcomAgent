"""RunService 组合测试:状态迁移与 AgentEvent 同事务(计划 02 Task 3;架构文档 §9)。"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.events.writer import RunNotFoundError
from runtime.execution.run_service import RunService, TransitionResult
from runtime.execution.transitions import InvalidTransition
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import RunStatus as PersistenceRunStatus
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.events import AgentEvent
from runtime.persistence.models.execution import Run
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import Session as SessionRow
from runtime.persistence.repositories.events import EventRepository
from runtime.persistence.repositories.runs import RunRepository


async def test_transition_run_moves_status_and_writes_event_in_same_transaction(
    engine: AsyncEngine,
    db_session: AsyncSession,
    run_service: RunService,
    seeded_run: Run,
    run_repository: RunRepository,
    events_repository: EventRepository,
    seeded_workspace,
) -> None:
    """迁移与事件在同一事务:flush 后同 Session 同时可见,提交前另一连接不可见。"""
    # 在任何 commit/rollback 前'捕获清理所需的 id(rollback 会使 ORM 属性过期,
    # 之后访问会触发同步 IO 的隐式刷新而失败)。
    cleanup_ids = {
        "workspace_id": seeded_workspace.workspace.id,
        "user_id": seeded_workspace.user.id,
        "membership_id": seeded_workspace.membership.id,
        "agent_id": seeded_workspace.agent.id,
        "agent_version_id": seeded_workspace.agent_version.id,
        "model_connection_id": seeded_workspace.model_config_version.model_connection_id,
        "model_config_version_id": seeded_workspace.model_config_version.id,
    }
    # 种子行先提交使另一连接可见;随后的迁移+事件保持未提交以验证同事务边界。
    await db_session.commit()
    try:
        result = await run_service.transition_run(
            run_id=seeded_run.id,
            target_status=RunStatus.RUNNING,
            event_kind="RUN_STARTED",
            event_payload={"attempt": 1},
        )
        assert isinstance(result, TransitionResult)
        assert result.previous_status == RunStatus.QUEUED
        assert result.target_status == RunStatus.RUNNING
        assert result.event.sequence_no == 1
        assert result.event.event_type == "RUN_STARTED"

        # 同一 Session 内(flush 未提交):状态与事件同时可读。
        run = await run_repository.get(seeded_workspace.workspace.id, seeded_run.id)
        assert run is not None
        assert run.status == PersistenceRunStatus.RUNNING
        assert run.state_version == 2
        events = await events_repository.list_for_run(
            workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
        )
        assert [event.event_type for event in events] == ["RUN_STARTED"]

        # 事务尚未提交:另一连接(READ COMMITTED)既看不到新状态也看不到事件。
        verify_session = AsyncSession(engine, expire_on_commit=False)
        try:
            verify_runs = RunRepository(verify_session)
            verify_events = EventRepository(verify_session)
            persisted_run = await verify_runs.get(seeded_workspace.workspace.id, seeded_run.id)
            assert persisted_run is not None
            assert persisted_run.status == PersistenceRunStatus.QUEUED
            assert persisted_run.state_version == 1
            assert (
                await verify_events.list_for_run(
                    workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
                )
                == []
            )
        finally:
            await verify_session.close()
    finally:
        # 先释放 db_session 持有的行锁(未提交的 CAS/事件写入),再清理种子行。
        await db_session.rollback()
        await _cleanup_committed_seed(engine, cleanup_ids)


async def test_transition_run_waiting_input_round_trip(
    run_service: RunService,
    seeded_run: Run,
    run_repository: RunRepository,
    events_repository: EventRepository,
    seeded_workspace,
) -> None:
    """QUEUED→RUNNING→WAITING_INPUT→QUEUED 全程合法,每次迁移各写一条事件。"""
    await run_service.transition_run(
        run_id=seeded_run.id,
        target_status=RunStatus.RUNNING,
        event_kind="RUN_STARTED",
        event_payload={},
    )
    await run_service.transition_run(
        run_id=seeded_run.id,
        target_status=RunStatus.WAITING_INPUT,
        event_kind="INPUT_REQUESTED",
        event_payload={"request_id": "req-1"},
    )
    result = await run_service.transition_run(
        run_id=seeded_run.id,
        target_status=RunStatus.QUEUED,
        event_kind="INPUT_ANSWERED",
        event_payload={"request_id": "req-1"},
    )
    assert result.previous_status == RunStatus.WAITING_INPUT

    run = await run_repository.get(seeded_workspace.workspace.id, seeded_run.id)
    assert run is not None
    assert run.status == PersistenceRunStatus.QUEUED
    assert run.state_version == 4
    events = await events_repository.list_for_run(
        workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
    )
    assert [event.sequence_no for event in events] == [1, 2, 3]
    assert [event.event_type for event in events] == [
        "RUN_STARTED",
        "INPUT_REQUESTED",
        "INPUT_ANSWERED",
    ]


async def test_transition_run_rejects_illegal_transition_and_writes_no_event(
    run_service: RunService,
    seeded_run: Run,
    events_repository: EventRepository,
    seeded_workspace,
) -> None:
    """QUEUED→COMPLETED 非法:抛 InvalidTransition 且不产生事件、不改状态。"""
    with pytest.raises(InvalidTransition) as exc_info:
        await run_service.transition_run(
            run_id=seeded_run.id,
            target_status=RunStatus.COMPLETED,
            event_kind="RUN_COMPLETED",
            event_payload={},
        )
    assert exc_info.value.current == RunStatus.QUEUED
    assert exc_info.value.target == RunStatus.COMPLETED
    assert await events_repository.list_for_run(
        workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
    ) == []


async def test_transition_run_rejects_stale_version(
    run_service: RunService,
    seeded_run: Run,
    events_repository: EventRepository,
    seeded_workspace,
) -> None:
    """CAS 失败(expected_version 过期)同样抛 InvalidTransition,且不产生事件。"""
    stale_version = seeded_run.state_version + 5
    with pytest.raises(InvalidTransition) as exc_info:
        await run_service.transition_run(
            run_id=seeded_run.id,
            target_status=RunStatus.RUNNING,
            event_kind="RUN_STARTED",
            event_payload={},
            expected_version=stale_version,
        )
    assert exc_info.value.current == RunStatus.QUEUED
    assert exc_info.value.target == RunStatus.RUNNING
    assert exc_info.value.reason is not None
    assert await events_repository.list_for_run(
        workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
    ) == []


async def test_transition_run_unknown_run_raises_not_found(run_service: RunService) -> None:
    """跨 workspace / 不存在的 run:抛 RunNotFoundError。"""
    with pytest.raises(RunNotFoundError):
        await run_service.transition_run(
            run_id=uuid.uuid4(),
            target_status=RunStatus.RUNNING,
            event_kind="RUN_STARTED",
            event_payload={},
        )


async def _cleanup_committed_seed(engine: AsyncEngine, ids: dict[str, uuid.UUID]) -> None:
    """按外键依赖倒序删除本测试提交的种子行,保持测试库干净。

    ``ids`` 必须在 ORM 属性过期前捕获(纯 UUID,而非 ORM 对象属性)。
    """
    workspace_id = ids["workspace_id"]
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await session.execute(
                sa.delete(AgentEvent).where(AgentEvent.workspace_id == workspace_id)
            )
            await session.execute(sa.delete(Run).where(Run.workspace_id == workspace_id))
            await session.execute(
                sa.delete(SessionRow).where(SessionRow.workspace_id == workspace_id)
            )
            await session.execute(
                sa.delete(AgentVersion).where(AgentVersion.id == ids["agent_version_id"])
            )
            await session.execute(
                sa.delete(ModelConfigVersion).where(
                    ModelConfigVersion.id == ids["model_config_version_id"]
                )
            )
            await session.execute(
                sa.delete(ModelConnection).where(
                    ModelConnection.id == ids["model_connection_id"]
                )
            )
            await session.execute(
                sa.delete(WorkspaceMembership).where(
                    WorkspaceMembership.id == ids["membership_id"]
                )
            )
            await session.execute(sa.delete(Agent).where(Agent.id == ids["agent_id"]))
            await session.execute(sa.delete(User).where(User.id == ids["user_id"]))
            await session.execute(sa.delete(Workspace).where(Workspace.id == workspace_id))
    finally:
        await session.close()
