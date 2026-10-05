"""Session 命令路由(计划 04 Task 3;架构文档《Runtime执行状态与恢复设计》§5)。

- POST /api/sessions:创建私有 Session,绑定当前 actor(非成员 403);
- GET /api/sessions/{id}:私有 Session 快照(非创建者/非成员一律 404);
  快照附带 PENDING Input/Confirmation Request(阶段 4 Gate:等待事件
  payload 只有 tool_call_id,Request 详情由快照提供,供工作台渲染);
- POST /api/sessions/{id}/messages:提交用户消息,创建 QUEUED Run 与
  ADVANCE_RUN Job;活动 Run 已存在时 409 ACTIVE_RUN_EXISTS。

命令为短事务:服务只 flush,路由返回前统一 commit;本模块不调用
Backend/ToolHandler/RuntimeHost 推进循环。
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.requirements import (
    ProductInputRequirementResolver,
)
from apps.api.command_errors import translated_commands
from apps.api.dependencies import (
    ActorDep,
    SessionDep,
    get_request_id,
    get_workspace_actor_context,
)
from apps.api.schemas.sessions import (
    AppendMessageRequest,
    CreateSessionRequest,
    MessageView,
    PendingConfirmationView,
    PendingInputOptionView,
    PendingInputView,
    RunDispatchView,
    RunSummaryView,
    SessionSnapshotView,
    SessionView,
)
from runtime.access.actor import ActorContextBuilder, ActorContextError
from runtime.persistence.base import (
    ConfirmationRequestStatus,
    InputRequestStatus,
)
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
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
    request: Request,
    actor: ActorDep,
    session: SessionDep,
) -> SessionSnapshotView:
    """私有 Session 快照;非创建者/非成员一律 404,不泄漏存在性。"""
    service = SessionCommandService(session, actor_id=actor.actor_id)
    with translated_commands():
        row = await service.get_session(session_id)
        messages = await service.session_messages(row)
        runs = await service.session_runs(row)
        active_run_id = await service.session_active_run_id(row)
    return SessionSnapshotView(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        title=row.title,
        status=row.status.value,
        created_by_actor_id=row.created_by_actor_id,
        active_run_id=active_run_id,
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
        pending_inputs=await _pending_inputs(
            request, session, row.workspace_id, row.id, actor.actor_id
        ),
        pending_confirmations=await _pending_confirmations(
            session, row.workspace_id, row.id
        ),
    )


async def _pending_inputs(
    request: Request,
    session: AsyncSession,
    workspace_id: UUID,
    session_id: UUID,
    actor_id: UUID,
) -> list[PendingInputView]:
    """PENDING Input Request 视图;店铺字段候选按当前 Actor 授权范围补齐。

    AgentLoop 创建 Request 时未持久化候选项(agent_loop 不传 options),
    ``store_id`` 的候选在快照侧按 Actor 授权店铺实时解析——不改变 runtime
    语义,只把领域候选呈现给工作台。
    """
    rows = (
        await session.scalars(
            select(InputRequest)
            .where(
                InputRequest.workspace_id == workspace_id,
                InputRequest.session_id == session_id,
                InputRequest.status == InputRequestStatus.PENDING,
            )
            .order_by(InputRequest.created_at, InputRequest.id)
        )
    ).all()
    if not rows:
        return []
    store_options = await _store_options(session, workspace_id, actor_id, request)
    views: list[PendingInputView] = []
    for row in rows:
        field_paths = [str(path) for path in row.field_paths]
        options = _options_of(row.options_json, field_paths) or (
            store_options if "store_id" in field_paths else []
        )
        views.append(
            PendingInputView(
                id=row.id,
                run_id=row.run_id,
                field_paths=field_paths,
                question=row.question,
                options=options,
                status=row.status.value,
            )
        )
    return views


async def _pending_confirmations(
    session: AsyncSession, workspace_id: UUID, session_id: UUID
) -> list[PendingConfirmationView]:
    """PENDING Confirmation Request 视图;参数来自关联 ToolCall 的挂起参数。"""
    rows = (
        await session.scalars(
            select(ConfirmationRequest)
            .where(
                ConfirmationRequest.workspace_id == workspace_id,
                ConfirmationRequest.session_id == session_id,
                ConfirmationRequest.status == ConfirmationRequestStatus.PENDING,
            )
            .order_by(ConfirmationRequest.created_at, ConfirmationRequest.id)
        )
    ).all()
    views: list[PendingConfirmationView] = []
    for row in rows:
        tool_call = (
            await session.scalars(
                select(ToolCall).where(
                    ToolCall.workspace_id == workspace_id,
                    ToolCall.id == row.tool_call_id,
                )
            )
        ).one_or_none()
        merged = (
            tool_call.normalized_arguments
            if tool_call is not None and tool_call.normalized_arguments is not None
            else (tool_call.raw_arguments if tool_call is not None else None)
        )
        views.append(
            PendingConfirmationView(
                id=row.id,
                run_id=row.run_id,
                normalized_arguments=dict(merged) if merged else None,
                resource_versions=dict(row.resource_versions),
                status=row.status.value,
            )
        )
    return views


def _options_of(
    options_json: Any, field_paths: list[str]
) -> list[PendingInputOptionView]:
    """摊平 Request 持久化候选项(options_json: field → [{value,label}])。"""
    if not isinstance(options_json, dict):
        return []
    options: list[PendingInputOptionView] = []
    for field in field_paths:
        raw = options_json.get(field)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict) and "value" in item:
                options.append(
                    PendingInputOptionView(
                        value=str(item["value"]),
                        label=str(item.get("label", item["value"])),
                    )
                )
    return options


async def _store_options(
    session: AsyncSession, workspace_id: UUID, actor_id: UUID, request: Request
) -> list[PendingInputOptionView]:
    """按当前 Actor 授权范围解析店铺候选;身份不可用时候选为空。"""
    try:
        actor = await ActorContextBuilder(session).build(
            actor_id=actor_id,
            workspace_id=workspace_id,
            request_id=get_request_id(request),
        )
    except ActorContextError:
        return []
    resolver = ProductInputRequirementResolver(session)
    options = await resolver.store_options(actor)
    return [
        PendingInputOptionView(value=option.value, label=option.label)
        for option in options
    ]


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
