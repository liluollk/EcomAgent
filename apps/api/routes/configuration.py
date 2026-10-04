"""Workspace 配置路由(计划 04 Task 2;架构文档 §3、§4.2、§8)。

覆盖:Execution Mode、模型连接(Secret 掩码返回、密文落库)、Skill、
Source 与 Business Policy。读取对任意有效成员开放;写入仅限
WorkspaceOwner;每次变更在同一事务内追加审计事件。
"""

from uuid import UUID

from fastapi import APIRouter

from apps.api.dependencies import SessionDep, WorkspaceActorDep, WorkspaceOwnerDep
from apps.api.schemas.workspaces import (
    CreateBusinessPolicyRequest,
    CreateModelConnectionRequest,
    CreateSkillRequest,
    CreateSourceRequest,
    ModelConnectionListResponse,
    SkillListResponse,
    SourceListResponse,
    UpdateExecutionModeRequest,
    UpdateModelConnectionRequest,
)
from apps.api.service_errors import translated
from runtime.workspace.service import (
    BusinessPolicyListResponse,
    BusinessPolicyView,
    ExecutionModeView,
    ModelConnectionView,
    SkillView,
    SourceView,
    WorkspaceService,
)


router = APIRouter()


# ---------------------------------------------------------------------------
# Execution Mode
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/execution-mode", response_model=ExecutionModeView)
async def get_execution_mode(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> ExecutionModeView:
    """返回当前执行模式与状态版本。"""
    service = WorkspaceService(session, actor)
    return await service.get_execution_mode(workspace_id)


@router.patch("/api/workspaces/{workspace_id}/execution-mode", response_model=ExecutionModeView)
async def update_execution_mode(
    workspace_id: UUID,
    payload: UpdateExecutionModeRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> ExecutionModeView:
    """修改执行模式;state_version 递增并追加审计事件。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.update_execution_mode(workspace_id, mode=payload.mode)
    await session.commit()
    return view


# ---------------------------------------------------------------------------
# 模型连接
# ---------------------------------------------------------------------------


@router.get(
    "/api/workspaces/{workspace_id}/model-connections", response_model=ModelConnectionListResponse
)
async def list_model_connections(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> ModelConnectionListResponse:
    """列出模型连接及其最新配置版本;Secret 只返回固定掩码。"""
    service = WorkspaceService(session, actor)
    connections = await service.list_model_connections(workspace_id)
    return ModelConnectionListResponse(model_connections=connections)


@router.post(
    "/api/workspaces/{workspace_id}/model-connections",
    response_model=ModelConnectionView,
    status_code=201,
)
async def create_model_connection(
    workspace_id: UUID,
    payload: CreateModelConnectionRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> ModelConnectionView:
    """创建模型连接并写入 v1 配置;api_key 加密落库,响应只含掩码。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.create_model_connection(
            workspace_id,
            stable_key=payload.stable_key,
            name=payload.name,
            backend_type=payload.backend_type,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key.get_secret_value(),
            non_secret_options=payload.non_secret_options,
        )
    await session.commit()
    return view


@router.get(
    "/api/workspaces/{workspace_id}/model-connections/{connection_id}",
    response_model=ModelConnectionView,
)
async def get_model_connection(
    workspace_id: UUID,
    connection_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> ModelConnectionView:
    """返回单个模型连接的最新配置版本;Secret 只返回固定掩码。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.get_model_connection(workspace_id, connection_id)
    return view


@router.patch(
    "/api/workspaces/{workspace_id}/model-connections/{connection_id}",
    response_model=ModelConnectionView,
)
async def update_model_connection(
    workspace_id: UUID,
    connection_id: UUID,
    payload: UpdateModelConnectionRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> ModelConnectionView:
    """创建新配置版本(发布版本不可原地更新);api_key 变更时轮换 Secret。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.update_model_connection(
            workspace_id,
            connection_id,
            name=payload.name,
            base_url=payload.base_url,
            model=payload.model,
            api_key=(
                payload.api_key.get_secret_value() if payload.api_key is not None else None
            ),
            non_secret_options=payload.non_secret_options,
        )
    await session.commit()
    return view


# ---------------------------------------------------------------------------
# Skill / Source / Business Policy
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/skills", response_model=SkillListResponse)
async def list_skills(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> SkillListResponse:
    """列出 Workspace Skill 及其最新版本。"""
    service = WorkspaceService(session, actor)
    skills = await service.list_skills(workspace_id)
    return SkillListResponse(skills=skills)


@router.post("/api/workspaces/{workspace_id}/skills", response_model=SkillView, status_code=201)
async def create_skill(
    workspace_id: UUID,
    payload: CreateSkillRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> SkillView:
    """创建 Workspace Skill 并写入 v1 内容版本。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.create_skill(
            workspace_id,
            stable_key=payload.stable_key,
            name=payload.name,
            content=payload.content,
        )
    await session.commit()
    return view


@router.get("/api/workspaces/{workspace_id}/sources", response_model=SourceListResponse)
async def list_sources(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> SourceListResponse:
    """列出 Workspace Source(如 MCP Server)及其最新版本。"""
    service = WorkspaceService(session, actor)
    sources = await service.list_sources(workspace_id)
    return SourceListResponse(sources=sources)


@router.post(
    "/api/workspaces/{workspace_id}/sources", response_model=SourceView, status_code=201
)
async def create_source(
    workspace_id: UUID,
    payload: CreateSourceRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> SourceView:
    """创建 Source 并写入 v1 配置版本。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.create_source(
            workspace_id,
            stable_key=payload.stable_key,
            kind=payload.kind,
            name=payload.name,
            config=payload.config,
        )
    await session.commit()
    return view


@router.get(
    "/api/workspaces/{workspace_id}/business-policies", response_model=BusinessPolicyListResponse
)
async def list_business_policies(
    workspace_id: UUID,
    actor: WorkspaceActorDep,
    session: SessionDep,
) -> BusinessPolicyListResponse:
    """列出 Workspace 业务规则及其最新版本。"""
    service = WorkspaceService(session, actor)
    policies = await service.list_business_policies(workspace_id)
    return BusinessPolicyListResponse(business_policies=policies)


@router.post(
    "/api/workspaces/{workspace_id}/business-policies",
    response_model=BusinessPolicyView,
    status_code=201,
)
async def create_business_policy(
    workspace_id: UUID,
    payload: CreateBusinessPolicyRequest,
    actor: WorkspaceOwnerDep,
    session: SessionDep,
) -> BusinessPolicyView:
    """创建业务规则并写入 v1 配置版本。"""
    service = WorkspaceService(session, actor)
    with translated():
        view = await service.create_business_policy(
            workspace_id,
            stable_key=payload.stable_key,
            name=payload.name,
            config=payload.config,
        )
    await session.commit()
    return view
