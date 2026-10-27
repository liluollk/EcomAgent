"""Session 命令路由(计划 04 Task 3;架构文档《Runtime执行状态与恢复设计》§5)。

- POST /api/sessions:创建私有 Session,绑定当前 actor(非成员 403);
- GET /api/sessions/{id}:私有 Session 快照(非创建者/非成员一律 404);
  快照附带 PENDING Input/Confirmation Request(阶段 4 Gate:等待事件
  payload 只有 tool_call_id,Request 详情由快照提供,供工作台渲染);
- POST /api/sessions/{id}/messages:提交用户消息,创建 QUEUED Run 与
  由进程内执行器推进;活动 Run 已存在时 409 ACTIVE_RUN_EXISTS。

命令为短事务:服务只 flush,路由返回前统一 commit;本模块不调用
Backend/ToolHandler/Runtime 推进循环。
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
from apps.api.errors import ApiError
from apps.api.executor_host import notify_run
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
from apps.model_driver import WorkspaceReadiness, check_workspace_readiness
from runtime.access.actor import ActorContextBuilder, ActorContextError
from runtime.commands.session import SessionCommandService
from runtime.persistence.base import (
    ConfirmationRequestStatus,
    InputRequestStatus,
)
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Session,
)


router = APIRouter()


@router.post("/api/sessions", response_model=SessionView, status_code=201)
async def create_session(
    payload: CreateSessionRequest,
    actor: ActorDep,
    session: SessionDep,
) -> SessionView:
    """创建私有 Session;Agent 版本由服务端按 Workspace 当前绑定解析。"""
    ctx = await get_workspace_actor_context(payload.workspace_id, actor, session)
    with translated_commands():
        created = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).create_session(ctx, title=payload.title)
    await session.commit()
    return SessionView(
        id=created.id,
        workspace_id=created.workspace_id,
        agent_id=created.agent_id,
        agent_version_id=created.agent_version_id,
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
    readiness = await _readiness_for(request, session, row.workspace_id)
    failure = None if readiness is None else readiness.failure
    return SessionSnapshotView(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        agent_version_id=row.agent_version_id,
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
        model_ready=None if readiness is None else readiness.ready,
        model_blocked_reason=None if failure is None else failure.message,
    )


async def _pending_inputs(
    request: Request,
    session: AsyncSession,
    workspace_id: UUID,
    session_id: UUID,
    actor_id: UUID,
) -> list[PendingInputView]:
    """PENDING Input Request 视图;候选项优先取持久化结果。

    AgentLoop 创建 Request 时已持久化服务端候选项(店铺、店铺引用、位置),
    此处直接摊平 ``options_json``;仅当历史数据没有候选项、且缺少的正是
    ``store_id`` 时,才按 Actor 授权店铺兜底实时解析。
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


async def _readiness_for(
    request: Request, session: AsyncSession, workspace_id: UUID
) -> WorkspaceReadiness | None:
    """真实模型 driver 下返回 Workspace 就绪状态;脚本 driver 返回 None(不适用)。

    返回 None 表示"这个概念不适用于当前装配",``ready=False`` 表示"确实缺配置",
    两者在快照里分别对应 ``model_ready=None`` 与 ``model_ready=False``。
    """
    driver = getattr(request.app.state, "executor_driver", None)
    if not getattr(driver, "requires_model_config", False):
        return None
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return None
    return await check_workspace_readiness(engine, workspace_id)


async def _require_workspace_ready(
    request: Request, session: AsyncSession, session_id: UUID
) -> None:
    """提交前校验执行前置条件;缺失项以 409 + 稳定错误码返回。

    只看 driver 是否需要模型配置(``requires_model_config``):演示入口的脚本
    driver 自带 Backend,不因为 Workspace 没配模型而被拦住。
    """
    if getattr(request.app.state, "executor_driver", None) is None:
        return
    workspace_id = await session.scalar(
        select(Session.workspace_id).where(Session.id == session_id)
    )
    if workspace_id is None:
        return  # 会话不存在:交给命令服务给出它自己的 404
    readiness = await _readiness_for(request, session, workspace_id)
    if readiness is None:
        return
    failure = readiness.failure
    if failure is not None:
        raise ApiError(status_code=409, code=failure.code, message=failure.message)


@router.post(
    "/api/sessions/{session_id}/messages",
    response_model=RunDispatchView,
    status_code=201,
)
async def append_message(
    session_id: UUID,
    payload: AppendMessageRequest,
    request: Request,
    actor: ActorDep,
    session: SessionDep,
) -> RunDispatchView:
    """提交用户消息:创建 QUEUED Run 并唤醒执行器;活动 Run 存在即 409。

    真实模型 driver 下,先确认该 Workspace 具备执行条件:没配模型或缺价格
    策略时直接 409 并说明缺什么,不创建注定失败的 Run。脚本 driver 不依赖
    模型配置,因此不做这项校验。
    """
    await _require_workspace_ready(request, session, session_id)
    with translated_commands():
        dispatched = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).append_message(
            session_id,
            content=payload.content,
            model_config_version_id=payload.model_config_version_id,
        )
    await session.commit()
    notify_run(request.app, dispatched.run.id)
    return RunDispatchView(
        message_id=dispatched.message.id,
        run_id=dispatched.run.id,
        status=dispatched.run.status.value,
    )
