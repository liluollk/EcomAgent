"""Workspace 隔离不变式测试(计划 01 Task 4 Step 1)。

- Repository 查询显式携带 workspace_id,绝不返回其他 Workspace 的行;
- 跨 Workspace 写入被复合外键拒绝;
- AgentEvent 按 Run 递增序号追加且 ``uq_event_sequence_per_run`` 阻止重复;
- UnitOfWork 拥有事务边界:正常退出不提交、异常退出回滚、显式 commit 才落库。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.persistence.base import ConfigScope, MessageRole, MessageStatus, ToolEffect
from runtime.persistence.models.configuration import ToolDefinition, ToolVersion
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.identity import Workspace
from runtime.persistence.models.interaction import Message
from runtime.persistence.unit_of_work import SqlAlchemyUnitOfWork


async def test_session_repository_never_returns_other_workspace(
    session_repository, seeded_session
) -> None:
    result = await session_repository.get(
        workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000099"),
        session_id=seeded_session.id,
    )
    assert result is None


async def test_session_repository_returns_session_for_own_workspace(
    session_repository, seeded_session
) -> None:
    result = await session_repository.get(
        workspace_id=seeded_session.workspace_id,
        session_id=seeded_session.id,
    )
    assert result is not None
    assert result.id == seeded_session.id
    assert result.workspace_id == seeded_session.workspace_id


async def _seed_tool_call(db_session: AsyncSession, seeded_run) -> ToolCall:
    """创建 message + 全局 tool 版本 + tool_call,供 confirmation request 外键引用。"""
    tool_definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        stable_key=f"tool-{uuid.uuid4()}",
        name="test-tool",
    )
    tool_version = ToolVersion(
        id=uuid.uuid4(),
        tool_id=tool_definition.id,
        version=1,
        effect=ToolEffect.READ,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        schema_hash="test-hash",
    )
    message = Message(
        id=uuid.uuid4(),
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_run.session_id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETED,
        content="user message",
        sequence_no=1,
    )
    tool_call = ToolCall(
        id=uuid.uuid4(),
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        message_id=message.id,
        tool_version_id=tool_version.id,
        ordinal=1,
        raw_arguments={},
    )
    # 无 relationship() 声明,按外键依赖分批 flush 保证插入顺序。
    db_session.add_all([tool_definition, tool_version, message])
    await db_session.flush()
    db_session.add(tool_call)
    await db_session.flush()
    return tool_call


async def test_request_repository_reads_only_own_workspace_requests(
    db_session, requests_repository, seeded_run
) -> None:
    tool_call = await _seed_tool_call(db_session, seeded_run)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)

    input_request = await requests_repository.append_input_request(
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        field_paths=["store.name"],
        question="请提供店铺名称",
        response_schema={"type": "object"},
        created_by_actor_id=seeded_run.created_by_actor_id,
        expires_at=expires_at,
    )
    confirmation_request = await requests_repository.append_confirmation_request(
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        tool_call_id=tool_call.id,
        actor_id=seeded_run.created_by_actor_id,
        fingerprint="fp-1",
        normalized_arguments_hash="hash-1",
        resource_versions={"store": "v1"},
        expires_at=expires_at,
    )

    fetched_input = await requests_repository.get_input_request(
        workspace_id=seeded_run.workspace_id, request_id=input_request.id
    )
    fetched_confirmation = await requests_repository.get_confirmation_request(
        workspace_id=seeded_run.workspace_id, request_id=confirmation_request.id
    )
    assert fetched_input is not None and fetched_input.id == input_request.id
    assert fetched_confirmation is not None and fetched_confirmation.id == confirmation_request.id
    assert (
        await requests_repository.get_input_request(
            workspace_id=uuid.uuid4(), request_id=input_request.id
        )
        is None
    )
    assert (
        await requests_repository.get_confirmation_request(
            workspace_id=uuid.uuid4(), request_id=confirmation_request.id
        )
        is None
    )


async def test_repository_never_writes_across_workspaces(
    db_session, events_repository, seeded_run
) -> None:
    """携带错误 workspace_id 的写入必须被复合外键拒绝。"""
    other_workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    db_session.add(other_workspace)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await events_repository.append(
            workspace_id=other_workspace.id,
            session_id=seeded_run.session_id,
            run_id=seeded_run.id,
            sequence_no=1,
            event_type="RUN_CREATED",
            payload={},
        )


async def test_event_sequence_starts_at_one_and_increments(events_repository, seeded_run) -> None:
    first = await events_repository.append(
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        sequence_no=1,
        event_type="RUN_CREATED",
        payload={"trigger": "user_message"},
    )
    second = await events_repository.append(
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        sequence_no=2,
        event_type="RUN_STARTED",
        payload={},
    )

    events = await events_repository.list_for_run(
        workspace_id=seeded_run.workspace_id, run_id=seeded_run.id
    )
    assert [event.sequence_no for event in events] == [1, 2]
    assert [event.id for event in events] == [first.id, second.id]


async def test_duplicate_event_sequence_rejected(events_repository, seeded_run) -> None:
    await events_repository.append(
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        sequence_no=1,
        event_type="RUN_CREATED",
        payload={},
    )
    with pytest.raises(IntegrityError):
        await events_repository.append(
            workspace_id=seeded_run.workspace_id,
            session_id=seeded_run.session_id,
            run_id=seeded_run.id,
            sequence_no=1,
            event_type="RUN_STARTED",
            payload={},
        )


async def _count_workspaces_named(engine: AsyncEngine, name: str) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.text("SELECT count(*) FROM workspaces WHERE name = :name"), {"name": name}
        )
    return int(result.scalar_one())


async def test_unit_of_work_does_not_commit_on_normal_exit(engine) -> None:
    uow = SqlAlchemyUnitOfWork(engine)
    async with uow:
        await uow.workspaces.create(name="uow-normal-exit")

    assert await _count_workspaces_named(engine, "uow-normal-exit") == 0


async def test_unit_of_work_rolls_back_on_exception(engine) -> None:
    uow = SqlAlchemyUnitOfWork(engine)
    with pytest.raises(RuntimeError):
        async with uow:
            await uow.workspaces.create(name="uow-exception-exit")
            raise RuntimeError("boom")

    assert await _count_workspaces_named(engine, "uow-exception-exit") == 0


async def test_unit_of_work_commit_persists(engine) -> None:
    async with SqlAlchemyUnitOfWork(engine) as uow:
        await uow.workspaces.create(name="uow-committed")
        await uow.commit()

    assert await _count_workspaces_named(engine, "uow-committed") == 1


def test_repositories_never_commit() -> None:
    """Repository 只允许 flush,绝不调用 commit(计划 01 Task 4 Step 3)。"""
    repository_files = Path("runtime/persistence/repositories").glob("*.py")
    violations = [
        path.name for path in repository_files if ".commit(" in path.read_text(encoding="utf-8")
    ]
    assert violations == []
