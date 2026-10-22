"""Input/Confirmation Request 命令路由(计划 04 Task 3;架构文档 §4.4、§5、§6)。

全部命令为单次处理:RequestService 条件更新 ``PENDING → 目标状态``;重复提交
409 REQUEST_NOT_PENDING,未声明字段 422 UNDECLARED_FIELD。回答/裁决成功后在
同一短事务内把 Run 从 WAITING_* 迁回 QUEUED,提交后唤醒进程内执行器——路由
本身不调用 Backend/ToolHandler/Runtime 推进循环。

路由路径不含 workspace:资源按当前 actor 的有效成员身份范围解析,不属于这些
工作区一律 404,不泄漏存在性。
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel

from apps.api.command_errors import translated_commands
from apps.api.dependencies import ActorDep, SessionDep
from apps.api.executor_host import notify_run
from apps.api.schemas.runs import RequestDecisionView
from runtime.commands.session import SessionCommandService


router = APIRouter()


class AnswerRequest(BaseModel):
    """POST /api/requests/{id}/answer 请求体;键必须都在 field_paths 内。"""

    values: dict[str, Any]


@router.post("/api/requests/{request_id}/answer", response_model=RequestDecisionView)
async def answer_request(
    request_id: UUID,
    payload: AnswerRequest,
    request: Request,
    actor: ActorDep,
    session: SessionDep,
) -> RequestDecisionView:
    """回答 Input Request:单次消费;成功后 Run 迁回 QUEUED 并调度推进。"""
    with translated_commands():
        decision = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).answer_input_request(request_id, dict(payload.values))
    await session.commit()
    notify_run(request.app, decision.run_id)
    return RequestDecisionView(
        request_id=decision.request_id,
        kind=decision.kind,
        status=decision.status,
        run_id=decision.run_id,
        run_status=decision.run_status,
    )


@router.post(
    "/api/requests/{request_id}/confirmation/accept",
    response_model=RequestDecisionView,
)
async def accept_confirmation(
    request_id: UUID,
    request: Request,
    actor: ActorDep,
    session: SessionDep,
) -> RequestDecisionView:
    """接受 Confirmation Request:单次消费;成功后 Run 迁回 QUEUED。"""
    with translated_commands():
        decision = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).accept_confirmation(request_id)
    await session.commit()
    notify_run(request.app, decision.run_id)
    return RequestDecisionView(
        request_id=decision.request_id,
        kind=decision.kind,
        status=decision.status,
        run_id=decision.run_id,
        run_status=decision.run_status,
    )


@router.post(
    "/api/requests/{request_id}/confirmation/reject",
    response_model=RequestDecisionView,
)
async def reject_confirmation(
    request_id: UUID,
    request: Request,
    actor: ActorDep,
    session: SessionDep,
) -> RequestDecisionView:
    """拒绝 Confirmation Request:单次消费;REJECTED 不执行 Tool。"""
    with translated_commands():
        decision = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).reject_confirmation(request_id)
    await session.commit()
    notify_run(request.app, decision.run_id)
    return RequestDecisionView(
        request_id=decision.request_id,
        kind=decision.kind,
        status=decision.status,
        run_id=decision.run_id,
        run_status=decision.run_status,
    )
