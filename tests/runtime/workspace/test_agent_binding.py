"""Workspace 唯一有效 AgentVersion 解析服务测试。"""

from __future__ import annotations

import importlib
import importlib.util
import uuid
from types import SimpleNamespace

import pytest

from apps.api.services import workspace_overview


def test_workspace_agent_resolver_module_exists() -> None:
    module_name = "runtime.workspace.agent_binding"

    assert importlib.util.find_spec(module_name) is not None
    module = importlib.import_module(module_name)

    assert hasattr(module, "WorkspaceAgentResolver")
    assert hasattr(module, "WorkspaceAgentNotConfigured")
    assert hasattr(module, "WorkspaceAgentConfigurationInvalid")


@pytest.mark.parametrize("row_count", [0, 2])
async def test_workspace_agent_resolver_rejects_missing_or_ambiguous_binding(
    row_count: int,
) -> None:
    module = importlib.import_module("runtime.workspace.agent_binding")
    rows = [object()] * row_count

    class Result:
        def all(self) -> list[object]:
            return rows

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

    resolver = module.WorkspaceAgentResolver(Session())
    expected = (
        module.WorkspaceAgentNotConfigured
        if row_count == 0
        else module.WorkspaceAgentConfigurationInvalid
    )

    with pytest.raises(expected):
        await resolver.resolve(uuid.uuid4())


async def test_workspace_agent_resolver_returns_the_single_effective_version() -> None:
    module = importlib.import_module("runtime.workspace.agent_binding")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        stable_key="product_operations",
        name="商品运营 Agent",
    )
    version = SimpleNamespace(id=uuid.uuid4(), version=3)
    row = (SimpleNamespace(), version, agent)

    class Result:
        def all(self) -> list[tuple[object, object, object]]:
            return [row]

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

    resolved = await module.WorkspaceAgentResolver(Session()).resolve(uuid.uuid4())

    assert resolved.agent_id == agent.id
    assert resolved.agent_version_id == version.id
    assert resolved.stable_key == "product_operations"
    assert resolved.name == "商品运营 Agent"
    assert resolved.version == 3


async def test_workspace_overview_uses_the_shared_agent_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()

    class Resolver:
        def __init__(self, _session: object) -> None:
            pass

        async def resolve(self, _workspace_id: uuid.UUID) -> object:
            return SimpleNamespace(
                agent_id=agent_id,
                agent_version_id=version_id,
                stable_key="product_operations",
                name="商品运营 Agent",
                version=2,
            )

    monkeypatch.setattr(
        workspace_overview,
        "WorkspaceAgentResolver",
        Resolver,
        raising=False,
    )
    service = workspace_overview.WorkspaceOverviewService.__new__(
        workspace_overview.WorkspaceOverviewService
    )
    service._session = object()

    result = await service._enabled_agent(uuid.uuid4())

    assert result is not None
    assert result.id == agent_id
    assert result.stable_key == "product_operations"
    assert result.version == 2
    assert result.status == "PUBLISHED"
