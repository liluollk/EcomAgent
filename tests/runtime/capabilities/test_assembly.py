"""Task 2:版本 Registry 与 RuntimeAssembly 测试(计划 02 Task 2)。

前四个为计划原文测试;其余覆盖启动校验补充场景:Agent 缺所需 Skill/Tool/
Source 版本、写 Tool 缺 Operation/Reconciliation 声明、Backend 能力不足、
MCP Tool 未本地分类不得启用、注册表重复注册与版本冲突。
"""

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import func, select

from runtime.backend.scripted import ScriptedBackend
from runtime.capabilities.assembly import RuntimeAssembly
from runtime.capabilities.registry import (
    AgentDefinition,
    AgentRegistry,
    AgentVersionRecord,
    BackendRegistry,
    SkillDefinition,
    SkillRegistry,
    SkillVersionRecord,
    SourceRegistry,
    SourceVersionRecord,
    ToolBinding,
    ToolRegistry,
    ToolVersionRecord,
)
from runtime.capabilities.run_factory import RunFactory
from runtime.capabilities.validation import AssemblyError, compute_schema_hash
from runtime.capabilities.write_service import ToolCallRequest
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.pretool.profiles import (
    PreToolProfileRegistration,
    no_op_profile,
)
from runtime.interfaces.tools import ToolDefinition, ToolEffect
from runtime.persistence.base import ConfigVersionStatus
from runtime.persistence.models.configuration import (
    BusinessPolicy,
    BusinessPolicyVersion,
)
from runtime.persistence.models.execution import (
    Run,
    RunPolicySnapshot,
    RunSkillVersion,
    RunSourceVersion,
    RunToolVersion,
)
from runtime.persistence.repositories.run_snapshots import (
    PolicySnapshotPin,
    RunSnapshotRepository,
)


AGENT_KEY = "product-maintenance"
PRICE_WRITE_PERMISSION = "product.price.write"

INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"sku": {"type": "string"}},
    "required": ["sku"],
}
OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"price": {"type": "string"}},
}
SCHEMA_HASH = compute_schema_hash(INPUT_SCHEMA, OUTPUT_SCHEMA)


def local_tool(
    key: str,
    *,
    effect: ToolEffect = ToolEffect.READ,
    required_permissions: frozenset[str] = frozenset(),
    schema_hash: str | None = None,
    version: int = 1,
    version_id: uuid.UUID | None = None,
) -> ToolVersionRecord:
    """构造 LOCAL Tool 版本记录;schema_hash 默认与 Schema 一致。"""
    return ToolVersionRecord(
        stable_key=key,
        version_id=version_id or uuid.uuid4(),
        version=version,
        workspace_id=None,
        source_kind="LOCAL",
        effect=effect,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        schema_hash=schema_hash if schema_hash is not None else SCHEMA_HASH,
        required_permissions=required_permissions,
    )


def mcp_tool(key: str, *, effect: ToolEffect | None = None) -> ToolVersionRecord:
    """构造 MCP Tool 版本记录;effect 为 None 表示未经本地分类。"""
    return ToolVersionRecord(
        stable_key=key,
        version_id=uuid.uuid4(),
        version=1,
        workspace_id=uuid.uuid4(),
        source_kind="MCP",
        effect=effect,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        schema_hash=SCHEMA_HASH,
        required_permissions=frozenset({"web.search"}) if effect is not None else frozenset(),
        source_version_id=uuid.uuid4(),
    )


def agent_record(
    *,
    agent_key: str = AGENT_KEY,
    tool_keys: frozenset[str] = frozenset(),
    skill_keys: frozenset[str] = frozenset(),
    source_keys: frozenset[str] = frozenset(),
    backend_capabilities: frozenset[str] = frozenset({"streaming", "tool_calling"}),
    version_id: uuid.UUID | None = None,
) -> AgentVersionRecord:
    return AgentVersionRecord(
        stable_key=agent_key,
        agent_id=uuid.uuid4(),
        version_id=version_id or uuid.uuid4(),
        version=1,
        definition=AgentDefinition(
            name=agent_key,
            system_prompt_version="v1",
            required_backend_capabilities=backend_capabilities,
            required_skill_keys=skill_keys,
            required_tool_keys=tool_keys,
            required_source_keys=source_keys,
        ),
    )


def skill_record(key: str, *, version_id: uuid.UUID | None = None) -> SkillVersionRecord:
    return SkillVersionRecord(
        stable_key=key,
        version_id=version_id or uuid.uuid4(),
        version=1,
        workspace_id=None,
        content=SkillDefinition(name=key),
    )


def source_record(
    key: str, *, version_id: uuid.UUID | None = None
) -> SourceVersionRecord:
    return SourceVersionRecord(
        stable_key=key,
        version_id=version_id or uuid.uuid4(),
        version=1,
        workspace_id=uuid.uuid4(),
        kind="MCP",
    )


class _StaticProfileFactory:
    """测试替身 Profile 工厂:固定返回空 Profile,不使用请求级上下文。"""

    async def create(self, context: object) -> object:
        del context
        return no_op_profile()


def _registration(*, policy_mode: str, resource_mode: str) -> PreToolProfileRegistration:
    return PreToolProfileRegistration(
        factory=_StaticProfileFactory(),
        policy_mode=policy_mode,  # type: ignore[arg-type]
        resource_mode=resource_mode,  # type: ignore[arg-type]
    )


def binding_for(
    key: str,
    *,
    effect: ToolEffect = ToolEffect.READ,
    required_permissions: frozenset[str] = frozenset(),
    write_adapter_capabilities: frozenset[str] = frozenset({"operation", "reconciliation"}),
) -> ToolBinding:
    is_write = effect is ToolEffect.WRITE
    return ToolBinding(
        definition=ToolDefinition(
            stable_key=key,
            version=1,
            description=key,
            input_json_schema=INPUT_SCHEMA,
            output_json_schema=OUTPUT_SCHEMA,
            effect=effect,
            required_permissions=required_permissions,
            source_kind="LOCAL",
            schema_hash=SCHEMA_HASH,
        ),
        handler=object(),
        pretool_registration=_registration(
            policy_mode="BUSINESS" if is_write else "NONE",
            resource_mode="RESOURCE" if is_write else "WORKSPACE",
        ),
        execution_profile=(
            ToolExecutionProfile(adapter_key="test-adapter") if is_write else None
        ),
        write_adapter_capabilities=write_adapter_capabilities,
    )


def make_assembly(
    *,
    tool_definitions: Sequence[ToolVersionRecord] = (),
    bindings: Sequence[ToolBinding] = (),
    agents: Sequence[AgentVersionRecord] | None = None,
    skills: Sequence[SkillVersionRecord] = (),
    sources: Sequence[SourceVersionRecord] = (),
    backend: ScriptedBackend | None = None,
) -> RuntimeAssembly:
    """按给定记录装配 RuntimeAssembly;缺省 Agent 要求全部给定 Tool。"""
    if agents is None:
        agents = (
            agent_record(tool_keys=frozenset(tool.stable_key for tool in tool_definitions)),
        )
    agent_registry = AgentRegistry()
    for record in agents:
        agent_registry.register(record)
    skill_registry = SkillRegistry()
    for record in skills:
        skill_registry.register(record)
    tool_registry = ToolRegistry()
    for record in tool_definitions:
        tool_registry.register(record)
    for binding in bindings:
        bound = tool_registry.find_by_key(binding.definition.stable_key)
        assert bound is not None
        tool_registry.bind(bound.version_id, binding)
    source_registry = SourceRegistry()
    for record in sources:
        source_registry.register(record)
    backend_registry = BackendRegistry()
    backend_registry.register(
        "scripted",
        backend
        if backend is not None
        else ScriptedBackend([], capability_keys=frozenset({"streaming", "tool_calling"})),
    )
    return RuntimeAssembly(
        agents=agent_registry,
        skills=skill_registry,
        tools=tool_registry,
        sources=source_registry,
        backends=backend_registry,
        backend_key="scripted",
    )


def make_assembly_with_mismatched_schema() -> RuntimeAssembly:
    """Tool 声明的 schema_hash 与注册 Schema 不一致。"""
    tool = local_tool("lookup", schema_hash="stale-hash")
    return make_assembly(tool_definitions=[tool], bindings=[binding_for("lookup")])


def make_price_tool_call() -> ToolCallRequest:
    return ToolCallRequest(
        tool_key="update_price",
        arguments={"store_id": "store-1", "sku": "SKU-1", "target_price": "12.34"},
    )


# ---- 计划原文测试 ----


def test_assembly_fails_when_local_tool_has_no_binding() -> None:
    assembly = make_assembly(tool_definitions=[local_tool("lookup")], bindings=[])
    with pytest.raises(AssemblyError, match="MISSING_TOOL_BINDING"):
        assembly.validate()


def test_assembly_fails_on_schema_hash_mismatch() -> None:
    assembly = make_assembly_with_mismatched_schema()
    with pytest.raises(AssemblyError, match="SCHEMA_HASH_MISMATCH"):
        assembly.validate()


async def test_run_pins_resolved_versions_but_not_live_authorization(
    run_factory, db_session
) -> None:
    run = await run_factory.create(agent_key=AGENT_KEY)
    assert run.tool_version_ids
    assert run.skill_version_ids
    assert run.source_version_ids
    assert run.permission_snapshot is not None
    # 权限快照固定创建时的授权视图;后续授权仍读当前库态。
    assert PRICE_WRITE_PERMISSION in run.permission_snapshot["permissions"]
    # 实际解析的版本写入 runs 与快照关联表。
    run_row = await db_session.get(Run, run.id)
    assert run_row is not None
    assert run_row.agent_version_id == run.agent_version_id
    pinned_tools = set(
        await db_session.scalars(
            select(RunToolVersion.tool_version_id).where(RunToolVersion.run_id == run.id)
        )
    )
    assert pinned_tools == set(run.tool_version_ids)
    pinned_skills = set(
        await db_session.scalars(
            select(RunSkillVersion.skill_version_id).where(RunSkillVersion.run_id == run.id)
        )
    )
    assert pinned_skills == set(run.skill_version_ids)
    pinned_sources = set(
        await db_session.scalars(
            select(RunSourceVersion.source_version_id).where(RunSourceVersion.run_id == run.id)
        )
    )
    assert pinned_sources == set(run.source_version_ids)


async def test_write_uses_current_authorization_after_run_creation(
    run_factory, membership_repository, write_service
) -> None:
    run = await run_factory.create(agent_key=AGENT_KEY)
    await membership_repository.revoke(run.actor_id, PRICE_WRITE_PERMISSION)
    result = await write_service.execute(run.id, make_price_tool_call())
    assert result.allowed is False
    assert result.denial_code == "PERMISSION_REVOKED"


# ---- Task 10:Run 钉住已发布的业务策略版本(PolicySnapshot) ----


async def test_run_pins_published_policy_versions(
    db_session, capability_catalog
) -> None:
    """Run 创建时把调用方给的 policy pin 落库,且重复 replace 不增行、互不串 Run。

    Runtime 只搬运 ``PolicySnapshotPin``(key/版本行/hash),不理解商品或价格字段;
    这里用一条真实的 ``BusinessPolicyVersion`` 行验证外键与幂等语义,而不是伪造
    值对象——否则"落库"这条路径根本没被证明。
    """
    workspace_id = capability_catalog.workspace.id
    policy = BusinessPolicy(
        id=uuid.uuid4(), workspace_id=workspace_id, stable_key="some-policy", name="业务策略"
    )
    db_session.add(policy)
    await db_session.flush()
    version_row = BusinessPolicyVersion(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        business_policy_id=policy.id,
        version=3,
        config_json={"any": "payload"},
        config_hash="f" * 64,
        status=ConfigVersionStatus.PUBLISHED,
    )
    db_session.add(version_row)
    await db_session.flush()
    pin = PolicySnapshotPin(
        policy_key="some-policy", version_id=version_row.id, config_hash="f" * 64
    )

    factory = RunFactory(
        session=db_session,
        assembly=capability_catalog.assembly,
        workspace_id=workspace_id,
        actor_id=capability_catalog.user.id,
        membership_id=capability_catalog.membership.id,
        model_config_version_id=capability_catalog.model_config_version.id,
        policy_pins=(pin,),
    )
    first = await factory.create(agent_key=AGENT_KEY)
    second = await factory.create(agent_key=AGENT_KEY)
    assert first.policy_pins == (pin,)

    # 每个 Run 各钉一行;第二个 Run 不会复用或覆盖第一个 Run 的快照。
    first_rows = await db_session.scalars(
        select(RunPolicySnapshot.proposed_version_id).where(
            RunPolicySnapshot.run_id == first.id
        )
    )
    assert list(first_rows) == [version_row.id]
    second_rows = await db_session.scalars(
        select(RunPolicySnapshot.proposed_version_id).where(
            RunPolicySnapshot.run_id == second.id
        )
    )
    assert list(second_rows) == [version_row.id]
    total = await db_session.scalar(
        select(func.count()).select_from(RunPolicySnapshot).where(
            RunPolicySnapshot.workspace_id == workspace_id
        )
    )
    assert total == 2

    # 重复 replace 同一 Run:仍是恰好一行,且读回等价 pin。
    repository = RunSnapshotRepository(db_session)
    await repository.replace(
        run_id=first.id, workspace_id=workspace_id, policy_pins=(pin,)
    )
    assert await repository.policy_pins(first.id) == [pin]
    deduped = await db_session.scalar(
        select(func.count()).select_from(RunPolicySnapshot).where(
            RunPolicySnapshot.run_id == first.id
        )
    )
    assert deduped == 1


# ---- 启动校验补充测试 ----


def test_assembly_fails_when_required_skill_version_missing() -> None:
    assembly = make_assembly(agents=[agent_record(skill_keys=frozenset({"price-adjustment"}))])
    with pytest.raises(AssemblyError, match="AGENT_SKILL_VERSION_MISSING"):
        assembly.validate()


def test_assembly_fails_when_required_source_version_missing() -> None:
    assembly = make_assembly(
        agents=[agent_record(source_keys=frozenset({"commerce-platform"}))],
    )
    with pytest.raises(AssemblyError, match="AGENT_SOURCE_VERSION_MISSING"):
        assembly.validate()


def test_assembly_fails_when_required_tool_version_missing() -> None:
    assembly = make_assembly(agents=[agent_record(tool_keys=frozenset({"lookup"}))])
    with pytest.raises(AssemblyError, match="AGENT_TOOL_VERSION_MISSING"):
        assembly.validate()


def test_assembly_fails_when_write_tool_lacks_adapter_capabilities() -> None:
    tool = local_tool(
        "update_price",
        effect=ToolEffect.WRITE,
        required_permissions=frozenset({PRICE_WRITE_PERMISSION}),
    )
    assembly = make_assembly(
        tool_definitions=[tool],
        bindings=[
            binding_for(
                "update_price",
                effect=ToolEffect.WRITE,
                required_permissions=frozenset({PRICE_WRITE_PERMISSION}),
                write_adapter_capabilities=frozenset({"operation"}),
            )
        ],
    )
    with pytest.raises(AssemblyError, match="WRITE_TOOL_CAPABILITY_MISSING"):
        assembly.validate()


def test_assembly_fails_when_backend_lacks_required_capabilities() -> None:
    assembly = make_assembly(
        tool_definitions=[local_tool("lookup")],
        bindings=[binding_for("lookup")],
        backend=ScriptedBackend([], capability_keys=frozenset({"streaming"})),
    )
    with pytest.raises(AssemblyError, match="BACKEND_CAPABILITY_INSUFFICIENT"):
        assembly.validate()


def test_unclassified_mcp_tool_is_rejected() -> None:
    assembly = make_assembly(tool_definitions=[mcp_tool("web-search")])
    with pytest.raises(AssemblyError, match="MCP_TOOL_UNCLASSIFIED"):
        assembly.validate()


def test_valid_assembly_passes_and_resolves_pinned_version_ids() -> None:
    tool = local_tool(
        "update_price",
        effect=ToolEffect.WRITE,
        required_permissions=frozenset({PRICE_WRITE_PERMISSION}),
    )
    skill = skill_record("price-adjustment")
    source = source_record("commerce-platform")
    agent = agent_record(
        tool_keys=frozenset({"update_price"}),
        skill_keys=frozenset({"price-adjustment"}),
        source_keys=frozenset({"commerce-platform"}),
    )
    assembly = make_assembly(
        tool_definitions=[tool],
        bindings=[
            binding_for(
                "update_price",
                effect=ToolEffect.WRITE,
                required_permissions=frozenset({PRICE_WRITE_PERMISSION}),
            )
        ],
        agents=[agent],
        skills=[skill],
        sources=[source],
    )
    assembly.validate()
    resolved = assembly.resolve(AGENT_KEY)
    assert resolved.agent.version_id == agent.version_id
    assert resolved.tool_version_ids == {tool.version_id}
    assert resolved.skill_version_ids == {skill.version_id}
    assert resolved.source_version_ids == {source.version_id}


def test_classified_mcp_tool_is_enabled() -> None:
    tool = local_tool("lookup")
    mcp = mcp_tool("web-search", effect=ToolEffect.READ)
    assembly = make_assembly(
        tool_definitions=[tool, mcp],
        bindings=[binding_for("lookup")],
        agents=[agent_record(tool_keys=frozenset({"lookup", "web-search"}))],
    )
    assembly.validate()


def test_registry_keeps_current_and_compatible_versions() -> None:
    """同一 stable key 的多个版本可并存;当前版本由显式注册决定。"""
    registry = ToolRegistry()
    v1 = local_tool("set_listing_price", version=1)
    v2 = local_tool("set_listing_price", version=2)

    registry.register(v1, make_current=False)
    registry.register(v2, make_current=True)

    assert registry.find_by_key("set_listing_price") == v2
    assert registry.find_by_version_id(v1.version_id) == v1
    assert registry.find_by_version_id(v2.version_id) == v2
    assert registry.find_by_key_version("set_listing_price", 1) == v1
    assert registry.records() == (v2,)
    assert registry.all_records() == (v1, v2)


def test_registry_rejects_same_key_and_version_with_different_ids() -> None:
    """同一 (stable key, version) 指向不同 Version ID 时启动失败。"""
    registry = ToolRegistry()
    registry.register(local_tool("search_products", version=3))
    with pytest.raises(AssemblyError, match="REGISTRY_VERSION_CONFLICT"):
        registry.register(local_tool("search_products", version=3))


def test_tool_registry_rejects_duplicate_version_id_binding() -> None:
    """同一 ToolVersion ID 只允许一个 ToolBinding。"""
    registry = ToolRegistry()
    record = local_tool("lookup")
    registry.register(record)
    registry.bind(record.version_id, binding_for("lookup"))
    with pytest.raises(AssemblyError, match="REGISTRY_DUPLICATE_KEY"):
        registry.bind(record.version_id, binding_for("lookup"))


def test_registry_rejects_version_conflict() -> None:
    registry = AgentRegistry()
    record = agent_record()
    registry.register(record)
    with pytest.raises(AssemblyError, match="REGISTRY_VERSION_CONFLICT"):
        registry.register(
            agent_record(agent_key="other-agent", version_id=record.version_id)
        )
