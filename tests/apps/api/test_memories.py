"""Memory Workbench API 测试(计划 04 Task 7)。

覆盖:GET /api/workspaces/{workspace_id}/memories 只返回当前 actor 自己的记忆、
POST 纠正与 DELETE Tombstone 仅限本人(他人 403 / 不存在 404),响应只暴露
安全字段,不泄漏内部与敏感元数据。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from apps.api.main import create_app
from runtime.access.tokens import issue_token
from runtime.memory.service import MemoryCandidate, MemoryService
from runtime.persistence.base import AccountStatus, MembershipRole
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.integrations import IntegrationOutbox, MemoryRecord


MEMORY_FIELDS = {
    "id",
    "kind",
    "subject_key",
    "content",
    "revision",
    "lifecycle_status",
    "supersedes_id",
    "valid_from",
    "expires_at",
    "created_at",
}


@dataclass(frozen=True)
class SeededWorkspace:
    """两个成员的 Workspace 种子:唯一 Owner 与一个 Operator。"""

    workspace: Workspace
    owner: User
    operator: User

    @property
    def id(self) -> UUID:
        return self.workspace.id


async def _cleanup(session: AsyncSession, seeded: SeededWorkspace) -> None:
    """按外键顺序删除种子与派生行;已删除的行重复删除无副作用。"""
    await session.execute(
        delete(IntegrationOutbox).where(
            IntegrationOutbox.workspace_id == seeded.id
        )
    )
    await session.execute(
        delete(MemoryRecord).where(MemoryRecord.workspace_id == seeded.id)
    )
    await session.execute(
        delete(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == seeded.id
        )
    )
    await session.execute(delete(Workspace).where(Workspace.id == seeded.id))
    await session.execute(
        delete(User).where(User.id.in_([seeded.owner.id, seeded.operator.id]))
    )
    await session.commit()


@pytest.fixture()
async def workspace(engine: AsyncEngine) -> AsyncIterator[SeededWorkspace]:
    """创建双成员 Workspace 种子并提交;结束后按外键顺序清理。"""
    owner = User(
        id=uuid.uuid4(),
        login=f"owner-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    operator = User(
        id=uuid.uuid4(),
        login=f"operator-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    ws = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:8]}")
    owner_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        user_id=owner.id,
        role=MembershipRole.WORKSPACE_OWNER,
        status=AccountStatus.ACTIVE,
    )
    operator_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        user_id=operator.id,
        role=MembershipRole.OPERATOR,
        status=AccountStatus.ACTIVE,
    )
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add_all(
            [owner, operator, ws, owner_membership, operator_membership]
        )
        await session.commit()
        seeded = SeededWorkspace(workspace=ws, owner=owner, operator=operator)
        yield seeded
    finally:
        await session.rollback()
        await _cleanup(session, seeded)  # type: ignore[possibly-undefined]
        await session.close()


async def _seed_memory(
    engine: AsyncEngine,
    seeded: SeededWorkspace,
    *,
    actor_id: UUID,
    content: str,
    subject_key: str,
) -> MemoryRecord:
    """经 MemoryService 落库一条记忆并提交;返回权威记录。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        service = MemoryService(session, workspace_id=seeded.id, actor_id=actor_id)
        assert (
            service.accept(
                MemoryCandidate(
                    kind="preference",
                    content=content,
                    subject_key=subject_key,
                    actor_id=actor_id,
                )
            )
            is True
        )
        record = (
            await session.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.workspace_id == seeded.id,
                    MemoryRecord.subject_key == subject_key,
                )
            )
        ).one()
        await session.commit()
        await session.refresh(record)
        return record
    finally:
        await session.close()


def _bearer(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


@pytest.fixture()
async def operator_client(
    engine: AsyncEngine, workspace: SeededWorkspace
) -> AsyncIterator[AsyncClient]:
    app = create_app(engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=_bearer(workspace.operator.id),
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# 计划语义:仅当前 actor 可见/可纠正/可删除自己的记忆
# ---------------------------------------------------------------------------


async def test_memories_list_returns_only_current_actors_memories(
    operator_client: AsyncClient, workspace: SeededWorkspace, engine: AsyncEngine
) -> None:
    owner_record = await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.owner.id,
        content="owner preference",
        subject_key="owner-pref",
    )
    operator_record = await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.operator.id,
        content="operator preference",
        subject_key="operator-pref",
    )
    response = await operator_client.get(f"/api/workspaces/{workspace.id}/memories")
    assert response.status_code == 200
    body: Any = response.json()
    ids = [item["id"] for item in body["memories"]]
    assert str(operator_record.id) in ids
    assert str(owner_record.id) not in ids


async def test_correct_own_memory(
    operator_client: AsyncClient, workspace: SeededWorkspace, engine: AsyncEngine
) -> None:
    record = await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.operator.id,
        content="prefer short replies",
        subject_key="operator-pref",
    )
    response = await operator_client.post(
        f"/api/workspaces/{workspace.id}/memories/{record.id}/corrections",
        json={"content": "prefer cautious changes"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "prefer cautious changes"
    assert body["revision"] == record.revision + 1
    assert body["supersedes_id"] == str(record.id)


async def test_correcting_another_users_memory_is_forbidden(
    operator_client: AsyncClient, workspace: SeededWorkspace, engine: AsyncEngine
) -> None:
    owner_record = await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.owner.id,
        content="owner preference",
        subject_key="owner-pref",
    )
    response = await operator_client.post(
        f"/api/workspaces/{workspace.id}/memories/{owner_record.id}/corrections",
        json={"content": "hijacked"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_correcting_missing_memory_is_not_found(
    operator_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    response = await operator_client.post(
        f"/api/workspaces/{workspace.id}/memories/{uuid.uuid4()}/corrections",
        json={"content": "prefer cautious changes"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_delete_own_memory_writes_tombstone(
    operator_client: AsyncClient, workspace: SeededWorkspace, engine: AsyncEngine
) -> None:
    record = await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.operator.id,
        content="prefer short replies",
        subject_key="operator-pref",
    )
    response = await operator_client.delete(
        f"/api/workspaces/{workspace.id}/memories/{record.id}"
    )
    assert response.status_code == 200
    assert response.json()["lifecycle_status"] == "DELETED"
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        row = await session.get(MemoryRecord, record.id)
        assert row is not None
        assert row.deleted_at is not None
    finally:
        await session.close()


async def test_deleting_another_users_memory_is_forbidden(
    operator_client: AsyncClient, workspace: SeededWorkspace, engine: AsyncEngine
) -> None:
    owner_record = await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.owner.id,
        content="owner preference",
        subject_key="owner-pref",
    )
    response = await operator_client.delete(
        f"/api/workspaces/{workspace.id}/memories/{owner_record.id}"
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_deleting_missing_memory_is_not_found(
    operator_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    response = await operator_client.delete(
        f"/api/workspaces/{workspace.id}/memories/{uuid.uuid4()}"
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_memory_response_exposes_only_safe_fields(
    operator_client: AsyncClient, workspace: SeededWorkspace, engine: AsyncEngine
) -> None:
    """响应不含敏感/内部字段:deleted/sync 元数据不外泄。"""
    await _seed_memory(
        engine,
        workspace,
        actor_id=workspace.operator.id,
        content="prefer short replies",
        subject_key="operator-pref",
    )
    response = await operator_client.get(f"/api/workspaces/{workspace.id}/memories")
    assert response.status_code == 200
    item = response.json()["memories"][0]
    assert set(item) == MEMORY_FIELDS
    assert "deleted_at" not in item
    assert "deleted_by_actor_id" not in item
    assert "sync_status" not in item
    assert item["content"] == "prefer short replies"
