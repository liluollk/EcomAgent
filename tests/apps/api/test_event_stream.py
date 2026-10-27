"""AgentEvent WebSocket 重放测试(计划 04 Task 4;架构文档 at-least-once 交付语义)。

覆盖计划原文测试 ``test_event_stream_replays_after_cursor`` 与补充语义:
- 端点选型:``WS /api/runs/{run_id}/events``;cursor 通过查询参数指定;
- cursor 语义:事件 UUID id 或整数序号(与 Run 快照 ``events_cursor`` 对齐),
  重放严格按 ``sequence_no`` 升序;
- 交付为 at-least-once:同一连接内服务端按 cursor 推进不重发;断线重连按
  cursor 重放可能重复,客户端按 event id 去重(测试以 ``_dedupe_by_id`` 模拟);
- 认证:无/无效 Bearer JWT 在握手前以 401 拒绝响应拒绝(不 accept);
  跨工作区/未知 Run 以 404 拒绝,不泄漏存在性;
- 可见性:事务提交后的新事件在轮询周期内被推送;
- 消息形态:``{id, sequence_no, event_kind, payload, created_at}`` 恰好五个
  字段,payload 与入库(已脱敏)内容逐字一致,流层不追加任何字段。

测试形态:starlette ``TestClient`` 的 ``WebSocketTestSession``(同步测试函数,
ASGI 内存传输,不监听真实端口)。种子与清理经 ``asyncio.run`` 在临时事件循环
中执行,应用运行在 TestClient portal 线程的独立事件循环;引擎使用 ``NullPool``
避免跨事件循环复用池化连接。
"""

import asyncio
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient, WebSocketDenialResponse, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from apps.api.main import create_app
from runtime.access.tokens import issue_token
from runtime.events.writer import AgentEventWriter
from runtime.persistence.base import (
    AccountStatus,
    MembershipRole,
    ModelBackendType,
    RunStatus,
)
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
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


@dataclass(frozen=True)
class SeededEvent:
    """种子事件的稳定标识(与推送 JSON 字段对应)。"""

    id: uuid.UUID
    sequence_no: int
    event_kind: str


@dataclass(frozen=True)
class SeededRun:
    """一个成员 + Session + Run + 事件序列的最小种子。"""

    workspace_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    user_id: uuid.UUID
    events: list[SeededEvent]
    cleanup_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    """清理所需的主键(agent/model 配置为全局或工作区级,须按 id 删除)。"""


@pytest.fixture()
def ws_engine(prepared_test_database: str) -> Iterator[AsyncEngine]:
    """NullPool 引擎:WS 应用运行在 TestClient portal 线程的独立事件循环,
    种子/清理运行在 ``asyncio.run`` 的临时循环;禁用连接池避免跨循环复用连接。"""
    engine = create_async_engine(prepared_test_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        asyncio.run(engine.dispose())


Seeder = Callable[..., SeededRun]


@pytest.fixture()
def seed_run(ws_engine: AsyncEngine) -> Iterator[Seeder]:
    """按需提交 Run 种子;测试结束后按外键倒序清理全部种子行。"""
    seeded: list[SeededRun] = []

    def _seed(*, event_count: int, status: str | None = None) -> SeededRun:
        row = _seed_run_with_events(ws_engine, event_count=event_count, status=status)
        seeded.append(row)
        return row

    try:
        yield _seed
    finally:
        for row in seeded:
            _cleanup(ws_engine, row)


def _seed_run_with_events(
    engine: AsyncEngine, *, event_count: int, status: str | None = None
) -> SeededRun:
    """提交 workspace/user/agent/model/session/run 种子与 event_count 条事件。"""

    async def _seed() -> SeededRun:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:8]}")
            user = User(
                id=uuid.uuid4(),
                login=f"user-{uuid.uuid4().hex[:12]}",
                password_hash="not-a-real-hash",
                status=AccountStatus.ACTIVE,
            )
            membership = WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.OPERATOR,
                status=AccountStatus.ACTIVE,
            )
            agent = Agent(
                id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4().hex[:12]}", name="test agent"
            )
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=1,
                definition_json={"role": "test"},
                definition_hash="test-definition-hash",
            )
            connection = ModelConnection(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                stable_key=f"conn-{uuid.uuid4().hex[:12]}",
                name="test model",
            )
            model_config_version = ModelConfigVersion(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                model_connection_id=connection.id,
                version=1,
                backend_type=ModelBackendType.OPENAI,
                base_url="https://model.invalid/v1",
                model="test-model",
                non_secret_options={},
                config_hash="test-config-hash",
            )
            session.add_all([workspace, user, agent])
            await session.flush()
            session.add_all([membership, agent_version, connection])
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
            if status is not None:
                # 终态 Run:流应在推送完已提交事件后结束,不再轮询。
                run.status = RunStatus(status)
                await session.flush()
            writer = AgentEventWriter(session=session, workspace_id=workspace.id)
            events = [
                _seeded(
                    await writer.append(run.id, f"TEST_EVENT_{index + 1}", {"index": index})
                )
                for index in range(event_count)
            ]
            return SeededRun(
                workspace_id=workspace.id,
                session_id=session_row.id,
                run_id=run.id,
                user_id=user.id,
                events=events,
                cleanup_ids={
                    "agent_id": agent.id,
                    "agent_version_id": agent_version.id,
                    "model_connection_id": connection.id,
                    "model_config_version_id": model_config_version.id,
                    "user_id": user.id,
                },
            )

    return asyncio.run(_seed())


def _seeded(appended: Any) -> SeededEvent:
    """AgentEventWriter 的只读视图 → 稳定种子事件。"""
    return SeededEvent(
        id=appended.event_id, sequence_no=appended.sequence_no, event_kind=appended.event_type
    )


def _append_committed_event(
    engine: AsyncEngine, seeded: SeededRun, event_kind: str
) -> SeededEvent:
    """以独立连接/事务追加并提交一条新事件(模拟 Runtime 侧提交后可见)。"""

    async def _append() -> SeededEvent:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            writer = AgentEventWriter(session=session, workspace_id=seeded.workspace_id)
            return _seeded(await writer.append(seeded.run_id, event_kind, {"late": True}))

    return asyncio.run(_append())


def _cleanup(engine: AsyncEngine, seeded: SeededRun) -> None:
    """按外键依赖倒序删除已提交种子行,保持测试库干净。"""
    ids = seeded.cleanup_ids

    async def _run() -> None:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            await session.execute(
                sa.delete(AgentEvent).where(AgentEvent.workspace_id == seeded.workspace_id)
            )
            await session.execute(
                sa.delete(Run).where(Run.workspace_id == seeded.workspace_id)
            )
            await session.execute(
                sa.delete(SessionRow).where(SessionRow.workspace_id == seeded.workspace_id)
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
                    WorkspaceMembership.workspace_id == seeded.workspace_id
                )
            )
            await session.execute(sa.delete(Agent).where(Agent.id == ids["agent_id"]))
            await session.execute(sa.delete(User).where(User.id == ids["user_id"]))
            await session.execute(
                sa.delete(Workspace).where(Workspace.id == seeded.workspace_id)
            )

    asyncio.run(_run())


def _stream_client(engine: AsyncEngine) -> TestClient:
    return TestClient(create_app(engine=engine))


def _ws_url(run_id: uuid.UUID, cursor: str | None = None) -> str:
    url = f"/api/runs/{run_id}/events"
    if cursor is not None:
        url = f"{url}?cursor={cursor}"
    return url


def _bearer(user_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


def _collect(
    session: WebSocketTestSession, count: int, *, timeout: float = 5.0
) -> list[dict[str, Any]]:
    """在守护线程收集 count 条推送;超时快速失败(不留存活线程)。"""
    result: Future[list[dict[str, Any]]] = Future()

    def _receive() -> None:
        result.set_result([session.receive_json() for _ in range(count)])

    threading.Thread(target=_receive, daemon=True).start()
    return result.result(timeout=timeout)


def _await_disconnect(session: WebSocketTestSession, *, timeout: float = 5.0) -> bool:
    """等待服务端关闭连接;在守护线程里读,避免阻塞主测试线程。"""
    result: Future[bool] = Future()

    def _receive() -> None:
        try:
            session.receive_json()
        except WebSocketDisconnect:
            result.set_result(True)
        except Exception as error:  # noqa: BLE001 — 任何异常都说明连接未正常关闭
            result.set_exception(error)

    threading.Thread(target=_receive, daemon=True).start()
    return result.result(timeout=timeout)


def _dedupe_by_id(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """客户端按 event id 去重(at-least-once 语义)。"""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for message in messages:
        if message["id"] not in seen:
            seen.add(message["id"])
            unique.append(message)
    return unique


# ---------------------------------------------------------------------------
# 计划原文测试(Task 4 Step 1)
# ---------------------------------------------------------------------------


def test_event_stream_replays_after_cursor(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    seeded = seed_run(event_count=4)
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id, str(seeded.events[1].id)),
        headers=_bearer(seeded.user_id),
    ) as ws:
        received = _dedupe_by_id(_collect(ws, len(seeded.events) - 2))
    assert [event["id"] for event in received] == [
        str(event.id) for event in seeded.events[2:]
    ]
    assert [event["sequence_no"] for event in received] == [
        event.sequence_no for event in seeded.events[2:]
    ]


# ---------------------------------------------------------------------------
# 重放语义:起点、序号 cursor、升序完整
# ---------------------------------------------------------------------------


def test_event_stream_replays_from_beginning_without_cursor(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    seeded = seed_run(event_count=3)
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id), headers=_bearer(seeded.user_id)
    ) as ws:
        received = _dedupe_by_id(_collect(ws, 3))
    assert [event["id"] for event in received] == [str(event.id) for event in seeded.events]
    assert [event["sequence_no"] for event in received] == [1, 2, 3]


def test_event_stream_accepts_sequence_no_cursor(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    """cursor 也可为整数序号(与 Run 快照 events_cursor 对齐)。"""
    seeded = seed_run(event_count=3)
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id, "2"), headers=_bearer(seeded.user_id)
    ) as ws:
        received = _dedupe_by_id(_collect(ws, 1))
    assert [event["id"] for event in received] == [str(seeded.events[2].id)]


def test_event_stream_rejects_malformed_and_unknown_cursor(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    seeded = seed_run(event_count=1)
    client = _stream_client(ws_engine)
    with pytest.raises(WebSocketDenialResponse) as malformed, client.websocket_connect(
        _ws_url(seeded.run_id, "not-a-cursor"), headers=_bearer(seeded.user_id)
    ):
        pass
    assert malformed.value.status_code == 422
    assert malformed.value.json()["code"] == "VALIDATION_ERROR"

    with pytest.raises(WebSocketDenialResponse) as unknown, client.websocket_connect(
        _ws_url(seeded.run_id, str(uuid.uuid4())), headers=_bearer(seeded.user_id)
    ):
        pass
    assert unknown.value.status_code == 404
    assert unknown.value.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 认证与工作区隔离(握手前拒绝)
# ---------------------------------------------------------------------------


def test_event_stream_requires_bearer_token(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    seeded = seed_run(event_count=1)
    client = _stream_client(ws_engine)

    with pytest.raises(WebSocketDenialResponse) as missing, client.websocket_connect(
        _ws_url(seeded.run_id)
    ):
        pass
    assert missing.value.status_code == 401
    assert missing.value.json()["code"] == "UNAUTHENTICATED"

    with pytest.raises(WebSocketDenialResponse) as invalid, client.websocket_connect(
        _ws_url(seeded.run_id), headers={"Authorization": "Bearer not-a-jwt"}
    ):
        pass
    assert invalid.value.status_code == 401
    assert invalid.value.json()["code"] == "TOKEN_INVALID"


def test_event_stream_rejects_run_outside_actor_workspaces(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    """有效身份但 Run 属于其它工作区:404,不泄漏存在性。"""
    mine = seed_run(event_count=1)
    other = seed_run(event_count=1)
    client = _stream_client(ws_engine)
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect(
        _ws_url(mine.run_id), headers=_bearer(other.user_id)
    ):
        pass
    assert denied.value.status_code == 404
    assert denied.value.json()["code"] == "RUN_NOT_FOUND"


def test_event_stream_rejects_unknown_run(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    seeded = seed_run(event_count=1)
    client = _stream_client(ws_engine)
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect(
        _ws_url(uuid.uuid4()), headers=_bearer(seeded.user_id)
    ):
        pass
    assert denied.value.status_code == 404
    assert denied.value.json()["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# 新事件可见性与消息形态
# ---------------------------------------------------------------------------


def test_event_stream_pushes_events_committed_after_connect(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    """连接建立并收到重放后,另一事务提交的新事件在轮询周期内被推送。"""
    seeded = seed_run(event_count=2)
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id), headers=_bearer(seeded.user_id)
    ) as ws:
        replayed = _dedupe_by_id(_collect(ws, 2))
        assert [event["sequence_no"] for event in replayed] == [1, 2]

        late = _append_committed_event(ws_engine, seeded, "TEST_EVENT_LATE")
        pushed = _dedupe_by_id(_collect(ws, 1))

    assert [event["id"] for event in pushed] == [str(late.id)]
    assert pushed[0]["event_kind"] == "TEST_EVENT_LATE"


def test_event_stream_message_shape_and_payload_are_verbatim(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    """推送 JSON 恰好五个字段;payload 与入库(已脱敏)内容逐字一致。"""
    seeded = seed_run(event_count=1)
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id), headers=_bearer(seeded.user_id)
    ) as ws:
        (message,) = _collect(ws, 1)

    assert set(message) == {"id", "sequence_no", "event_kind", "payload", "created_at"}
    assert message["id"] == str(seeded.events[0].id)
    assert message["sequence_no"] == 1
    assert message["event_kind"] == "TEST_EVENT_1"
    assert message["payload"] == {"index": 0}
    assert isinstance(datetime.fromisoformat(message["created_at"]), datetime)


def test_event_stream_ends_when_run_is_terminal(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    """Run 已终态时,推送完已提交事件后服务端主动结束连接。

    此前生成器是无限轮询:Run 早就结束,每个仍打开的页面还在按
    poll_interval 永久查询数据库,连接也一直挂着。
    """
    seeded = seed_run(event_count=2, status="COMPLETED")
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id), headers=_bearer(seeded.user_id)
    ) as ws:
        received = _collect(ws, 2)
        assert [message["sequence_no"] for message in received] == [1, 2]
        assert _await_disconnect(ws) is True


def test_event_stream_stays_open_for_non_terminal_run(
    seed_run: Seeder, ws_engine: AsyncEngine
) -> None:
    """非终态 Run 的连接必须保持打开,不能因为一次空批就结束。"""
    seeded = seed_run(event_count=1)
    client = _stream_client(ws_engine)
    with client.websocket_connect(
        _ws_url(seeded.run_id), headers=_bearer(seeded.user_id)
    ) as ws:
        assert _collect(ws, 1)[0]["sequence_no"] == 1
        # 空批轮询后仍存活:补一条事件应当能推到。
        _append_committed_event(ws_engine, seeded, "TEST_EVENT_LATE")
        assert _collect(ws, 1)[0]["event_kind"] == "TEST_EVENT_LATE"
