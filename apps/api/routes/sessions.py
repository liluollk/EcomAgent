"""Session 命令路由(计划 04 Task 3;架构文档《Runtime执行状态与恢复设计》§5)。

- POST /api/sessions:创建私有 Session,绑定当前 actor(非成员 403);
- GET /api/sessions/{id}:私有 Session 快照(非创建者/非成员一律 404);
- POST /api/sessions/{id}/messages:提交用户消息,创建 QUEUED Run 与
  ADVANCE_RUN Job;活动 Run 已存在时 409 ACTIVE_RUN_EXISTS。

命令为短事务:服务只 flush,路由返回前统一 commit;本模块不调用
Backend/ToolHandler/RuntimeHost 推进循环。
"""

from uuid import UUID

from fastapi import APIRouter

from apps.api.command_errors import translated_commands
from apps.api.dependencies import ActorDep, SessionDep, get_workspace_actor_context
from apps.api.schemas.sessions import (
    AppendMessageRequest,
    CreateSessionRequest,
    MessageView,
    RunDispatchView,
    RunSummaryView,
    SessionSnapshotView,
    SessionView,
)
from runtime.workspace.session_commands import SessionCommandService


router = APIRouter()


@router.post("/api/sessions", response_model=SessionView, status_code=201)
async def create_session(
    payload: CreateSessionRequest,
    actor: ActorDep,
    session: SessionDep,
) -> SessionView:
    """创建私有 Session;非工作区成员 403,agent 不存在 404。"""
    ctx = await get_workspace_actor_context(payload.workspace_id, actor, session)
    with translated_commands():
        created = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).create_session(ctx, agent_id=payload.agent_id, title=payload.title)
    await session.commit()
    return SessionView(
        id=created.id,
        workspace_id=created.workspace_id,
        agent_id=created.agent_id,
        title=created.title,
        status=created.status.value,
        created_by_actor_id=created.created_by_actor_id,
    )


@router.get("/api/sessions/{session_id}", response_model=SessionSnapshotView)
async def get_session(
    session_id: UUID,
    actor: ActorDep,
    session: SessionDep,
) -> SessionSnapshotView:
    """私有 Session 快照;非创建者/非成员一律 404,不泄漏存在性。"""
    service = SessionCommandService(session, actor_id=actor.actor_id)
    with translated_commands():
        row = await service.get_session(session_id)
        messages = await service.session_messages(row)
        runs = await service.session_runs(row)
    return SessionSnapshotView(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        title=row.title,
        status=row.status.value,
        created_by_actor_id=row.created_by_actor_id,
        active_run_id=row.active_run_id,
        messages=[
            MessageView(
                id=message.id,
                run_id=message.run_id,
                role=message.role.value,
                status=message.status.value,
                content=message.content,
                sequence_no=message.sequence_no,
            )
            for message in messages
        ],
        runs=[
            RunSummaryView(
                id=run.id, status=run.status.value, created_at=run.created_at
            )
            for run in runs
        ],
    )


@router.post(
    "/api/sessions/{session_id}/messages",
    response_model=RunDispatchView,
    status_code=201,
)
async def append_message(
    session_id: UUID,
    payload: AppendMessageRequest,
    actor: ActorDep,
    session: SessionDep,
) -> RunDispatchView:
    """提交用户消息:创建 QUEUED Run 与 ADVANCE_RUN Job;活动 Run 存在即 409。"""
    with translated_commands():
        dispatched = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).append_message(
            session_id,
            content=payload.content,
            model_config_version_id=payload.model_config_version_id,
        )
    await session.commit()
    return RunDispatchView(
        message_id=dispatched.message.id,
        run_id=dispatched.run.id,
        status=dispatched.run.status.value,
    )
