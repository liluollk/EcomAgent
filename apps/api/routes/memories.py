"""Memory Workbench 路由(计划 04 Task 7;架构文档 §7)。

应用服务与 API 仅允许当前 Actor 查询、纠正和删除自己的记忆:他人记忆
403 PERMISSION_DENIED,不存在的记忆 404 NOT_FOUND,纠正内容含疑似凭据
422,生命周期冲突 409。响应模型不携带 deleted/sync 等内部元数据。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import SessionDep, WorkspaceActorDep
from apps.api.errors import ApiError
from apps.api.schemas.memories import (
    CorrectMemoryRequest,
    MemoryListResponse,
    MemoryView,
)
from runtime.access.actor import ActorContext
from runtime.memory.lifecycle import MemoryLifecycleError
from runtime.memory.service import MemoryService, MemoryServiceError
from runtime.persistence.models.integrations import MemoryRecord


router = APIRouter()


@contextmanager
def _translated() -> Iterator[None]:
    """把 Memory 领域错误翻译为统一 ApiError;其余异常原样穿透。"""
    try:
        yield
    except (MemoryServiceError, MemoryLifecycleError) as error:
        raise ApiError(
            status_code=error.status_code, code=error.code, message=str(error)
        ) from error


def _to_view(record: MemoryRecord) -> MemoryView:
    return MemoryView(
        id=record.id,
        kind=record.kind,
        subject_key=record.subject_key,
        content=record.content,
        revision=record.revision,
        lifecycle_status=record.lifecycle_status.value,
        supersedes_id=record.supersedes_id,
        valid_from=record.valid_from,
        expires_at=record.expires_at,
        created_at=record.created_at,
    )


@router.get(
    "/api/workspaces/{workspace_id}/memories",
    response_model=MemoryListResponse,
)
async def list_memories(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> MemoryListResponse:
    """返回当前 actor 自己的记忆(含生命周期状态);他人的记忆不可见。"""
    service = _memory_service(session, actor)
    records = await service.list_own()
    return MemoryListResponse(memories=[_to_view(record) for record in records])


@router.post(
    "/api/workspaces/{workspace_id}/memories/{memory_id}/corrections",
    response_model=MemoryView,
)
async def correct_memory(
    workspace_id: UUID,
    memory_id: UUID,
    payload: CorrectMemoryRequest,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> MemoryView:
    """纠正当前 actor 自己的记忆:创建新 Revision 并替代旧记录。"""
    service = _memory_service(session, actor)
    with _translated():
        corrected = await service.correct(memory_id, content=payload.content)
    await session.commit()
    await session.refresh(corrected)
    return _to_view(corrected)


@router.delete(
    "/api/workspaces/{workspace_id}/memories/{memory_id}",
    response_model=MemoryView,
)
async def delete_memory(
    workspace_id: UUID,
    memory_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> MemoryView:
    """删除当前 actor 自己的记忆:写 Tombstone 并产生 MEMORY_DELETE Outbox。"""
    service = _memory_service(session, actor)
    with _translated():
        deleted = await service.delete(memory_id)
    await session.commit()
    await session.refresh(deleted)
    return _to_view(deleted)


def _memory_service(session: AsyncSession, actor: ActorContext) -> MemoryService:
    return MemoryService(
        session, workspace_id=actor.workspace_id, actor_id=actor.actor_id
    )
