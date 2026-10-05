"""Agent 目录读取路由(阶段 4 Gate;工作台创建 Session 需要 agent_id)。

只读端点:返回数据库内全部 Agent(当前仅组合根引导的商品运营 Agent)。
创建 Session 的 POST /api/sessions 请求体要求 agent_id,工作台经本端点
展示可用的 Agent 列表。
"""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from apps.api.dependencies import ActorDep, SessionDep
from runtime.persistence.models.configuration import Agent


router = APIRouter()


class AgentSummaryView(BaseModel):
    """Agent 目录项。"""

    id: UUID
    stable_key: str
    name: str


class AgentListResponse(BaseModel):
    """GET /api/agents 响应。"""

    agents: list[AgentSummaryView]


@router.get("/api/agents", response_model=AgentListResponse)
async def list_agents(
    actor: ActorDep,
    session: SessionDep,
) -> AgentListResponse:
    """返回当前账号可见的 Agent 目录(认证通过即可读;不分工作区)。"""
    del actor
    rows = (
        await session.scalars(select(Agent).order_by(Agent.created_at, Agent.id))
    ).all()
    return AgentListResponse(
        agents=[
            AgentSummaryView(id=agent.id, stable_key=agent.stable_key, name=agent.name)
            for agent in rows
        ]
    )
