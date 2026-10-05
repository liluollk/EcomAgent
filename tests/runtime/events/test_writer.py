"""AgentEventWriter 测试(计划 02 Task 3;架构文档 §9)。"""

import asyncio
import dataclasses
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.events.writer import AgentEventWriter, AppendedEvent, RunNotFoundError
from runtime.persistence.base import MembershipRole, ModelBackendType
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
from runtime.persistence.repositories.sessions import SessionRepository


async def test_event_sequence_is_unique_under_concurrency(
    event_writer: AgentEventWriter, seeded_run: Run
) -> None:
    """计划原文:并发 append 10 条事件,sequence_no 排序后恰为 1..10。

    单一 AsyncSession(单连接)下 asyncio.gather 实际串行执行;跨连接的
    真实并发竞争由 ``test_event_sequence_is_unique_across_real_connections``
    单独覆盖。
    """
    events = await asyncio.gather(
        *[
            event_writer.append(seeded_run.id, "TEST_EVENT", {"index": index})
            for index in range(10)
        ]
    )
    assert sorted(event.sequence_no for event in events) == list(range(1, 11))


async def test_event_sequence_is_unique_across_real_connections(engine: AsyncEngine) -> None:
    """真实并发:10 个独立连接/事务同时 append,序号仍恰为 1..10。

    序号分配依赖 ``UPDATE runs SET next_event_sequence = next_event_sequence + 1
    RETURNING`` 的行锁串行化;种子行先提交供其它连接可见,结束后清理。
    """
    ids = await _seed_committed(engine)
    try:

        async def worker(index: int) -> AppendedEvent:
            session = AsyncSession(engine, expire_on_commit=False)
            try:
                async with session.begin():
                    writer = AgentEventWriter(session=session, workspace_id=ids["workspace_id"])
                    return await writer.append(ids["run_id"], "TEST_EVENT", {"index": index})
            finally:
                await session.close()

        events = await asyncio.gather(*(worker(index) for index in range(10)))
        assert sorted(event.sequence_no for event in events) == list(range(1, 11))

        verify_session = AsyncSession(engine, expire_on_commit=False)
        try:
            persisted = await EventRepository(verify_session).list_for_run(
                workspace_id=ids["workspace_id"], run_id=ids["run_id"]
            )
            assert [event.sequence_no for event in persisted] == list(range(1, 11))
        finally:
            await verify_session.close()
    finally:
        await _cleanup_committed(engine, ids)


async def test_append_allocates_monotonic_sequence(
    event_writer: AgentEventWriter, seeded_run: Run
) -> None:
    """首次 append 得到 sequence_no=1,随后单调递增。"""
    first = await event_writer.append(seeded_run.id, "RUN_STARTED", {"attempt": 1})
    second = await event_writer.append(seeded_run.id, "RUN_COMPLETED", {"attempt": 1})
    assert (first.sequence_no, second.sequence_no) == (1, 2)


async def test_append_returns_read_only_view(
    event_writer: AgentEventWriter, seeded_run: Run
) -> None:
    """append 返回含 sequence_no/payload 的只读视图,不暴露 ORM 对象。"""
    appended = await event_writer.append(seeded_run.id, "RUN_STARTED", {"attempt": 1})
    assert isinstance(appended, AppendedEvent)
    assert appended.event_type == "RUN_STARTED"
    assert appended.payload == {"attempt": 1}
    assert appended.sequence_no == 1
    assert appended.run_id == seeded_run.id
    assert appended.workspace_id == seeded_run.workspace_id
    assert appended.session_id == seeded_run.session_id
    assert appended.schema_version == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        appended.sequence_no = 99  # type: ignore[misc]


async def test_duplicate_sequence_no_rejected_by_unique_index(
    events_repository: EventRepository,
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
) -> None:
    """(run_id, sequence_no) 唯一:重复序号由 uq_event_sequence_per_run 拒绝。"""
    await events_repository.append(
        workspace_id=seeded_workspace.workspace.id,
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        sequence_no=1,
        event_type="RUN_STARTED",
        payload={},
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await events_repository.append(
                workspace_id=seeded_workspace.workspace.id,
                session_id=seeded_run.session_id,
                run_id=seeded_run.id,
                sequence_no=1,
                event_type="RUN_STARTED",
                payload={},
            )


async def test_append_rejects_run_from_other_workspace(
    db_session: AsyncSession, seeded_run: Run
) -> None:
    """writer 绑定 workspace:对其它 workspace 的 run 分配序号必须失败。"""
    other_workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    db_session.add(other_workspace)
    await db_session.flush()

    writer = AgentEventWriter(session=db_session, workspace_id=other_workspace.id)
    with pytest.raises(RunNotFoundError):
        await writer.append(seeded_run.id, "TEST_EVENT", {})


async def test_list_for_run_is_isolated_by_workspace(
    events_repository: EventRepository,
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    event_writer: AgentEventWriter,
) -> None:
    """跨 workspace 读取隔离:其它 workspace 按 run_id 读不到事件。"""
    await event_writer.append(seeded_run.id, "RUN_STARTED", {})

    other_workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    db_session.add(other_workspace)
    await db_session.flush()

    leaked = await events_repository.list_for_run(
        workspace_id=other_workspace.id, run_id=seeded_run.id
    )
    assert leaked == []
    own = await events_repository.list_for_run(
        workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
    )
    assert [event.sequence_no for event in own] == [1]


async def _seed_committed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """提交最小种子行(workspace/user/.../session/run),返回关键 id 供并发与清理使用。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
            user = User(id=uuid.uuid4(), login=f"user-{uuid.uuid4()}", password_hash="test-hash")
            membership = WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.OPERATOR,
            )
            agent = Agent(id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4()}", name="test-agent")
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=1,
                definition_json={},
                definition_hash="test-hash",
            )
            model_connection = ModelConnection(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                stable_key=f"model-{uuid.uuid4()}",
                name="test-model",
            )
            model_config_version = ModelConfigVersion(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                model_connection_id=model_connection.id,
                version=1,
                backend_type=ModelBackendType.OPENAI,
                base_url="https://api.example.com/v1",
                model="gpt-test",
                non_secret_options={},
                config_hash="test-hash",
            )
            # 按外键依赖分批 flush,保证被引用行先落库。
            session.add_all([workspace, user, agent])
            await session.flush()
            session.add_all([membership, agent_version, model_connection])
            await session.flush()
            session.add(model_config_version)
            await session.flush()
            session_row = await SessionRepository(session).create(
                workspace_id=workspace.id,
                agent_id=agent.id,
                agent_version_id=agent_version.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
            )
            run = await RunRepository(session).create(
                workspace_id=workspace.id,
                session_id=session_row.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
                agent_version_id=agent_version.id,
                model_config_version_id=model_config_version.id,
            )
            return {
                "workspace_id": workspace.id,
                "user_id": user.id,
                "membership_id": membership.id,
                "agent_id": agent.id,
                "agent_version_id": agent_version.id,
                "model_connection_id": model_connection.id,
                "model_config_version_id": model_config_version.id,
                "run_id": run.id,
            }
    finally:
        await session.close()


async def _cleanup_committed(engine: AsyncEngine, ids: dict[str, uuid.UUID]) -> None:
    """按外键依赖倒序删除已提交的种子行,保持测试库干净。"""
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
