"""Run 命令与快照路由(计划 04 Task 3;架构文档《Runtime执行状态与恢复设计》§4.1)。

- GET /api/runs/{id}:Run 快照(status/messages/tool_calls/events cursor);
- POST /api/runs/{id}/cancel:经 transitions 合法路径迁移 CANCELLED,并尽力
  取消仍在等待的 Request;终态再取消 409 INVALID_TRANSITION。

资源按当前 actor 的有效成员身份范围解析;不属于这些工作区一律 404。
"""

from uuid import UUID

from fastapi import APIRouter

from apps.api.command_errors import translated_commands
from apps.api.dependencies import ActorDep, SessionDep
from apps.api.schemas.runs import RunSnapshotView, RunStatusView, ToolCallView
from apps.api.schemas.sessions import MessageView
from runtime.workspace.session_commands import SessionCommandService


router = APIRouter()


@router.get("/api/runs/{run_id}", response_model=RunSnapshotView)
async def get_run(
    run_id: UUID,
    actor: ActorDep,
    session: SessionDep,
) -> RunSnapshotView:
    """Run 快照;``events_cursor`` 指向最后已持久的事件序号。"""
    with translated_commands():
        snapshot = await SessionCommandService(
            session, actor_id=actor.actor_id
        ).get_run(run_id)
    run = snapshot.run
    return RunSnapshotView(
        id=run.id,
        session_id=run.session_id,
        workspace_id=run.workspace_id,
        status=run.status.value,
        state_version=run.state_version,
        events_cursor=snapshot.events_cursor,
        messages=[
            MessageView(
                id=message.id,
                run_id=message.run_id,
                role=message.role.value,
                status=message.status.value,
                content=message.content,
                sequence_no=message.sequence_no,
            )
            for message in snapshot.messages
        ],
        tool_calls=[
            ToolCallView(
                id=tool_call.id,
                ordinal=tool_call.ordinal,
                status=tool_call.status.value,
                raw_arguments=dict(tool_call.raw_arguments),
                failure_code=tool_call.failure_code,
            )
            for tool_call in snapshot.tool_calls
        ],
    )


@router.post("/api/runs/{run_id}/cancel", response_model=RunStatusView)
async def cancel_run(
    run_id: UUID,
    actor: ActorDep,
    session: SessionDep,
) -> RunStatusView:
    """取消 Run:CAS 迁移 CANCELLED;终态再取消 409 INVALID_TRANSITION。"""
    with translated_commands():
        run = await SessionCommandService(session, actor_id=actor.actor_id).cancel_run(
            run_id
        )
    await session.commit()
    return RunStatusView(id=run.id, status=run.status.value)
