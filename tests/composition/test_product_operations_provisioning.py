"""Provisioning 与 Composition 的边界(ADR-002 P0;Provisioning 计划 Task 1)。

发布(Provisioning)是唯一写 Agent/Skill/Tool/Model 版本化配置的入口;
普通组合根 ``build_product_application`` 只消费已发布快照,不再写配置表,
也不得凭不存在的 Version ID 自动补建配置行。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
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
    load_published_pricing_policy,
    pricing_policy_config_hash,
    product_tool_definitions,
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
from runtime.persistence.models.configuration import BusinessPolicyVersion
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
    "business_policies",
    "business_policy_versions",
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

    first = await provisioner.provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    after_first = await _configuration_counts(engine)
    second = await provisioner.provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )

    assert second == first
    assert await _configuration_counts(engine) == after_first


def test_current_tool_catalog_contains_only_p1_tools_and_runtime_control() -> None:
    """新 Run 的 current 目录不得继续暴露旧业务 Tool。"""
    assert {key for key, _ in product_tool_definitions()} == {
        "load_skill",
        "search_products",
        "get_listing",
        "compare_listing_prices",
        "set_listing_price",
        "get_inventory_level",
        "adjust_inventory_quantity",
        "publish_listing",
        "unpublish_listing",
    }


async def test_provision_returns_current_and_compatibility_versions(
    engine: AsyncEngine,
) -> None:
    """发布结果同时携带新 Run 快照与旧 Run 恢复所需精确 Version ID。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id,
        model=_MODEL_SPEC,
        pricing_policy=_PRICE_CONFIG,
    )

    assert capabilities.compatibility_agent_version_ids
    assert capabilities.compatibility_skill_version_ids
    assert set(capabilities.compatibility_tool_version_ids) == {
        "search_products",
        "get_product_snapshot",
        "compare_listing_prices",
        "update_price",
    }
    assert not (
        set(capabilities.tool_version_ids)
        & {
            "get_product_snapshot",
            "update_price",
        }
    )


async def test_build_product_application_is_read_only(engine: AsyncEngine) -> None:
    """普通装配只消费已发布快照:不新增任何配置行。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    before = await _configuration_counts(engine)

    application = await build_product_application(
        engine,
        capabilities=capabilities,
        backend=_StubBackend(),
        workspace_id=workspace_id,
        actor_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
    )
    after = await _configuration_counts(engine)

    assert after == before
    await application.aclose()


async def test_build_registers_all_compatibility_versions_for_recovery(
    engine: AsyncEngine,
) -> None:
    """历史 Run 按精确 Version ID 仍能解析旧 Agent、Skill 与 ToolBinding。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    application = await build_product_application(
        engine,
        capabilities=capabilities,
        backend=_StubBackend(),
        workspace_id=workspace_id,
        actor_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
    )
    try:
        assembly = application.assembly
        for version_id in capabilities.compatibility_agent_version_ids:
            assert assembly.agents.find_by_version_id(version_id) is not None
        for version_id in capabilities.compatibility_skill_version_ids:
            assert assembly.skills.find_by_version_id(version_id) is not None
        for key, version_ids in capabilities.compatibility_tool_version_ids.items():
            for version_id in version_ids:
                record = assembly.tools.find_by_version_id(version_id)
                assert record is not None
                assert record.stable_key == key
                assert assembly.tools.binding_for_version(version_id) is not None
    finally:
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
        pricing_policy_version_id=uuid.uuid4(),
        pricing_policy_config_hash="0" * 64,
    )

    with pytest.raises(AssemblyError):
        await build_product_application(
            engine,
            capabilities=unpublished,
            backend=_StubBackend(),
            workspace_id=workspace_id,
            actor_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
        )

    assert await _configuration_counts(engine) == before


# ---- P1 Task 10:价格策略作为已发布配置进入装配 ----

_ALT_PRICE_CONFIG: Final = PricePolicyConfig(
    policy_version="price-policy-v2",
    currency="CNY",
    min_allowed_price=Decimal("60.00"),
    max_price_change=Decimal("80.00"),
)


async def test_provision_publishes_pricing_policy_idempotently(
    engine: AsyncEngine,
) -> None:
    """同一份价格配置复用同一已发布版本;配置变化才递增版本并保留旧版本。"""
    workspace_id = await _seed_workspace(engine)
    provisioner = _provisioner(engine)

    first = await provisioner.provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    again = await provisioner.provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    assert again.pricing_policy_version_id == first.pricing_policy_version_id
    assert again.pricing_policy_config_hash == first.pricing_policy_config_hash

    changed = await provisioner.provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_ALT_PRICE_CONFIG
    )
    assert changed.pricing_policy_version_id != first.pricing_policy_version_id
    assert changed.pricing_policy_config_hash != first.pricing_policy_config_hash


async def test_loader_returns_published_policy_for_the_snapshot(
    engine: AsyncEngine,
) -> None:
    """按能力快照读取已发布策略:精确行 + 已解析配置。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )

    published = await load_published_pricing_policy(
        engine,
        workspace_id=workspace_id,
        version_id=capabilities.pricing_policy_version_id,
        expected_config_hash=capabilities.pricing_policy_config_hash,
    )

    assert published.config == _PRICE_CONFIG
    assert published.version == 1
    assert published.version_id == capabilities.pricing_policy_version_id


async def test_build_rejects_missing_published_pricing_policy(
    engine: AsyncEngine,
) -> None:
    """能力快照指向不存在的策略行:装配必须失败闭合,不接受"没有价格规则"。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    missing = replace(capabilities, pricing_policy_version_id=uuid.uuid4())

    with pytest.raises(AssemblyError, match="PRICING_POLICY_NOT_PUBLISHED"):
        await build_product_application(
            engine,
            capabilities=missing,
            backend=_StubBackend(),
            workspace_id=workspace_id,
            actor_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
        )


async def test_build_rejects_pricing_policy_with_mismatched_hash(
    engine: AsyncEngine,
) -> None:
    """行存在但 hash 与快照不符:说明策略行被改过,装配必须拒绝而不是照用。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    tampered = replace(capabilities, pricing_policy_config_hash="0" * 64)

    with pytest.raises(AssemblyError, match="PRICING_POLICY_NOT_PUBLISHED"):
        await build_product_application(
            engine,
            capabilities=tampered,
            backend=_StubBackend(),
            workspace_id=workspace_id,
            actor_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
        )


async def test_build_rejects_pricing_policy_owned_by_another_workspace(
    engine: AsyncEngine,
) -> None:
    """策略行属于别的 Workspace:跨租户读规则必须失败闭合。"""
    owner_workspace = await _seed_workspace(engine)
    other_workspace = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=owner_workspace, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )

    with pytest.raises(AssemblyError, match="PRICING_POLICY_NOT_PUBLISHED"):
        await build_product_application(
            engine,
            capabilities=capabilities,
            backend=_StubBackend(),
            workspace_id=other_workspace,
            actor_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
        )


async def test_build_rejects_pricing_policy_payload_that_is_not_a_config(
    engine: AsyncEngine,
) -> None:
    """行的 hash 自洽但载荷不是合法配置:``PRICING_POLICY_CONFIG_INVALID``。"""
    workspace_id = await _seed_workspace(engine)
    capabilities = await _provisioner(engine).provision(
        workspace_id=workspace_id, model=_MODEL_SPEC, pricing_policy=_PRICE_CONFIG
    )
    bogus_payload = {"policy_version": "price-policy-v1", "unexpected": "field"}
    bogus_hash = pricing_policy_config_hash(bogus_payload)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            row = await session.get(
                BusinessPolicyVersion, capabilities.pricing_policy_version_id
            )
            assert row is not None
            row.config_json = bogus_payload
            row.config_hash = bogus_hash
    finally:
        await session.close()
    corrupted = replace(capabilities, pricing_policy_config_hash=bogus_hash)

    with pytest.raises(AssemblyError, match="PRICING_POLICY_CONFIG_INVALID"):
        await build_product_application(
            engine,
            capabilities=corrupted,
            backend=_StubBackend(),
            workspace_id=workspace_id,
            actor_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
        )
