from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from runtime.access.actor import ActorContext
from runtime.commands import session as session_commands
from runtime.persistence.base import MembershipRole


async def test_session_creation_uses_server_resolved_workspace_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    captured: dict[str, object] = {}

    class Resolver:
        def __init__(self, _session: object) -> None:
            pass

        async def resolve(self, resolved_workspace_id: uuid.UUID) -> object:
            assert resolved_workspace_id == workspace_id
            return SimpleNamespace(
                agent_id=agent_id,
                agent_version_id=agent_version_id,
            )

    class Sessions:
        async def create(self, **values: object) -> object:
            captured.update(values)
            return SimpleNamespace(**values)

    monkeypatch.setattr(
        session_commands, "WorkspaceAgentResolver", Resolver, raising=False
    )
    service = session_commands.SessionCommandService(object(), actor_id=actor_id)
    service._sessions = Sessions()
    ctx = ActorContext(
        actor_id=actor_id,
        workspace_id=workspace_id,
        membership_id=membership_id,
        role=MembershipRole.WORKSPACE_OWNER,
        permissions=frozenset(),
        resource_scopes={},
        request_id="request-1",
    )

    await service.create_session(ctx, title="商品维护")

    assert captured == {
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "agent_version_id": agent_version_id,
        "created_by_actor_id": actor_id,
        "created_by_membership_id": membership_id,
        "title": "商品维护",
    }
