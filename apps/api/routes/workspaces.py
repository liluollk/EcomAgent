"""Workspace 详情与成员管理路由(计划 04 Task 2;架构文档 §3、§5)。

读取端点对任意有效成员开放;成员新增、角色变更与移除仅限 WorkspaceOwner。
移除或降级最后一个 WorkspaceOwner 返回 409 LAST_OWNER_REQUIRED。
"""

from uuid import UUID

from fastapi import APIRouter

from apps.api.dependencies import SessionDep, WorkspaceActorDep, WorkspaceOwnerDep
from apps.api.schemas.workspaces import (
    AddMemberRequest,
    MembershipListResponse,
    MembershipView,
    UpdateMemberRoleRequest,
    WorkspaceView,
)
from apps.api.service_errors import translated
from runtime.workspace.service import MembershipDetailView, WorkspaceService


router = APIRouter()


@router.get("/api/workspaces/{workspace_id}", response_model=WorkspaceView)
async def get_workspace(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> WorkspaceView:
    """返回 Workspace 详情;非成员一律 403,不泄漏存在性。"""
    service = WorkspaceService(session, actor)
    return await service.get_workspace(workspace_id)


@router.get(
    "/api/workspaces/{workspace_id}/memberships", response_model=MembershipListResponse
)
async def list_memberships(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> MembershipListResponse:
    """返回 Workspace 全部成员身份。"""
    service = WorkspaceService(session, actor)
    memberships = await service.list_members(workspace_id)
    return MembershipListResponse(memberships=memberships)


@router.post(
    "/api/workspaces/{workspace_id}/memberships",
    response_model=MembershipView,
    status_code=201,
)
async def add_membership(
    workspace_id: UUID,
    payload: AddMemberRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> MembershipView:
    """把已有用户加入 Workspace;仅 WorkspaceOwner。"""
    service = WorkspaceService(session, actor)
    with translated():
        membership = await service.add_member(
            workspace_id, user_id=payload.user_id, role=payload.role
        )
    await session.commit()
    return membership


@router.patch(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}",
    response_model=MembershipDetailView,
)
async def change_membership_role(
    workspace_id: UUID,
    membership_id: UUID,
    payload: UpdateMemberRoleRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> MembershipDetailView:
    """修改成员角色;降级最后一个 Owner 返回 409 LAST_OWNER_REQUIRED。"""
    service = WorkspaceService(session, actor)
    with translated():
        detail = await service.change_member_role(
            workspace_id, membership_id, role=payload.role
        )
    await session.commit()
    return detail


@router.delete(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}", status_code=204
)
async def remove_membership(
    workspace_id: UUID,
    membership_id: UUID,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> None:
    """移除成员;移除最后一个 Owner 返回 409 LAST_OWNER_REQUIRED。"""
    service = WorkspaceService(session, actor)
    with translated():
        await service.remove_member(workspace_id, membership_id)
    await session.commit()
