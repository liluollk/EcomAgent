"""Provisioning 与 Composition 的边界(ADR-002 P0;Provisioning 计划 Task 1)。

发布(Provisioning)是唯一写 Agent/Skill/Tool/Model 版本化配置的入口;
普通组合根 ``build_product_application`` 只消费已发布快照,不再写配置表,
也不得凭不存在的 Version ID 自动补建配置行。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.policy.models import PricePolicyConfig
from composition.product_operations import build_product_application
from composition.provisioning.product_operations import (
    ModelConnectionProvisioningSpec,
    ProductOperationsCapabilities,
    ProductOperationsProvisioner,
)
from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)
from runtime.capabilities.validation import AssemblyError
from runtime.persistence.base import ModelBackendType
from runtime.persistence.models.identity import Workspace


_MODEL_SPEC: Final = ModelConnectionProvisioningSpec(
    stable_key="product-scripted",
    backend_type=ModelBackendType.OPENAI,
    base_url="https://scripted.model.invalid/v1",
    model="product-scripted",
    non_secret_options={},
)

_PRICE_CONFIG: Final = PricePolicyConfig(
    policy_version="price-policy-v1",
    currency="CNY",
    min_allowed_price=Decimal("50.00"),
    max_price_change=Decimal("100.00"),
)

_CONFIG_TABLES: Final = (
    "agents",
    "agent_versions",
    "skills",
    "skill_versions",
    "tool_definitions",
    "tool_versions",
    "model_connections",
    "model_config_versions",
)


class _StubBackend:
    """最小模型后端:不产出任何事件,仅满足组装与启动校验。"""

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        del request, cancellation
        yield BackendCompleted(finish_reason="stop")


def _provisioner(engine: AsyncEngine) -> ProductOperationsProvisioner:
    return ProductOperationsProvisioner(
        lambda: AsyncSession(engine, expire_on_commit=False)
    )


async def _seed_workspace(engine: AsyncEngine) -> uuid.UUID:
    """模型连接外键要求存在 Workspace 行(能力行本身是全局的)。"""
    workspace_id = uuid.uuid4()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            session.add(Workspace(id=workspace_id, name=f"provisioning-{workspace_id.hex[:8]}"))
    finally:
        await session.close()
    return workspace_id


async def _configuration_counts(engine: AsyncEngine) -> dict[str, int]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        counts: dict[str, int] = {}
        for table in _CONFIG_TABLES:
            result = await session.execute(sa.text(f"SELECT count(*) FROM {table}"))
            counts[table] = int(result.scalar_one())
        return counts
    finally:
        await session.close()


async def test_provision_is_idempotent(engine: AsyncEngine) -> None:
    """同一 workspace、同一版本规范连续发布两次:同快照、行数不增长。"""
    workspace_id = await _seed_workspace(engine)
    provisioner = _provisioner(engine)

    first = await provisioner.provision(workspace_id=workspace_id, model=_MODEL_SPEC)
    after_first = await _configuration_counts(engine)
    second = await provisioner.provision(workspace_id=workspace_id, model=_MODEL_SPEC)

    assert second == first
    assert await _configuration_counts(engine) == after_first


async def test_build_product_application_is_read_only(engine: AsyncEngine) -> None:
    """普通装配只消费已发布快照:不新增任何配置行。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC
    )
    before = await _configuration_counts(engine)

    application = await build_product_application(
        engine,
        capabilities=capabilities,
        backend=_StubBackend(),
        workspace_id=workspace_id,
        actor_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        price_config=_PRICE_CONFIG,
    )
    after = await _configuration_counts(engine)

    assert after == before
    await application.aclose()


async def test_unpublished_snapshot_is_rejected(engine: AsyncEngine) -> None:
    """不存在的 Version ID 必须抛 AssemblyError,且数据库不自动补建配置行。"""
    workspace_id = await _seed_workspace(engine)
    before = await _configuration_counts(engine)
    unpublished = ProductOperationsCapabilities(
        agent_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        skill_id=uuid.uuid4(),
        skill_version_id=uuid.uuid4(),
        tool_definition_ids={},
        tool_version_ids={},
        model_config_version_id=uuid.uuid4(),
    )

    with pytest.raises(AssemblyError):
        await build_product_application(
            engine,
            capabilities=unpublished,
            backend=_StubBackend(),
            workspace_id=workspace_id,
            actor_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            price_config=_PRICE_CONFIG,
        )

    assert await _configuration_counts(engine) == before
