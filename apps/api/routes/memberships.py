"""成员授权管理路由(计划 04 Task 2;架构文档 §3、§4)。

成员的 Action Permission 与 Resource Scope 由 WorkspaceOwner 配置;
授予/收回立即改变后续授权判定(Authorize 结果由当前库态决定)。
"""

from uuid import UUID

from fastapi import APIRouter

from apps.api.dependencies import SessionDep, WorkspaceActorDep, WorkspaceOwnerDep
from apps.api.schemas.workspaces import GrantPermissionRequest, SetResourceScopeRequest
from apps.api.service_errors import translated
from runtime.workspace.service import MembershipDetailView, WorkspaceService


router = APIRouter()


@router.get(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}",
    response_model=MembershipDetailView,
)
async def get_membership(
    workspace_id: UUID,
    membership_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> MembershipDetailView:
    """返回成员详情,含动作权限与每类资源的范围。"""
    service = WorkspaceService(session, actor)
    return await service.get_membership(workspace_id, membership_id)


@router.post(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}/permissions",
    response_model=MembershipDetailView,
)
async def grant_permission(
    workspace_id: UUID,
    membership_id: UUID,
    payload: GrantPermissionRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> MembershipDetailView:
    """授予动作权限(幂等);仅 WorkspaceOwner。"""
    service = WorkspaceService(session, actor)
    with translated():
        detail = await service.grant_permission(
            workspace_id, membership_id, permission=payload.permission
        )
    await session.commit()
    return detail


@router.delete(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}/permissions/{permission}",
    response_model=MembershipDetailView,
)
async def revoke_permission(
    workspace_id: UUID,
    membership_id: UUID,
    permission: str,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> MembershipDetailView:
    """收回动作权限;未授予时 404。"""
    service = WorkspaceService(session, actor)
    with translated():
        detail = await service.revoke_permission(
            workspace_id, membership_id, permission=permission
        )
    await session.commit()
    return detail


@router.put(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}/resource-scopes",
    response_model=MembershipDetailView,
)
async def set_resource_scope(
    workspace_id: UUID,
    membership_id: UUID,
    payload: SetResourceScopeRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> MembershipDetailView:
    """设置一类资源的范围模式与 SELECTED 集合(整体替换);仅 WorkspaceOwner。"""
    service = WorkspaceService(session, actor)
    with translated():
        detail = await service.set_resource_scope(
            workspace_id,
            membership_id,
            resource_type=payload.resource_type,
            mode=payload.mode,
            resource_ids=payload.resource_ids,
        )
    await session.commit()
    return detail


@router.delete(
    "/api/workspaces/{workspace_id}/memberships/{membership_id}/resource-scopes/{resource_type}",
    response_model=MembershipDetailView,
)
async def revoke_resource_scope(
    workspace_id: UUID,
    membership_id: UUID,
    resource_type: str,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> MembershipDetailView:
    """收回一类资源的范围(模式与 SELECTED 集合一并删除);未配置时 404。"""
    service = WorkspaceService(session, actor)
    with translated():
        detail = await service.revoke_resource_scope(
            workspace_id, membership_id, resource_type=resource_type
        )
    await session.commit()
    return detail
