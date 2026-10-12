"""Task 2 测试夹具(计划 02 Task 2)。

提供:
- ``capability_catalog``:先按 ORM 插入能力目录种子行(与生产装配方一致:
  装配数据来自 DB),再构造进程内注册表与已校验的 RuntimeAssembly;
- ``run_factory``:钉版本 Run 创建服务(调用方事务内执行);
- ``membership_repository``:直接操作 membership_permissions 的测试辅助,
  提供 计划原文 的 ``revoke(actor_id, permission)`` 语义;
- ``write_service``:写前读当前授权的最小服务。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
from runtime.capabilities.validation import compute_schema_hash
from runtime.capabilities.write_service import WriteAuthorizationService
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.pretool.profiles import (
    PreToolProfileRegistration,
    no_op_profile,
)
from runtime.interfaces.tools import ToolDefinition, ToolEffect
from runtime.persistence.base import MembershipRole, ModelBackendType


class _StaticProfileFactory:
    """测试替身 Profile 工厂:固定返回空 Profile。"""

    async def create(self, context: object) -> object:
        del context
        return no_op_profile()
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
    Skill,
    SkillVersion,
    Source,
    SourceVersion,
)
from runtime.persistence.models.configuration import (
    ToolDefinition as ToolDefinitionRow,
)
from runtime.persistence.models.configuration import (
    ToolVersion as ToolVersionRow,
)
from runtime.persistence.models.identity import (
    MembershipPermission,
    User,
    Workspace,
    WorkspaceMembership,
)


AGENT_KEY = "product-maintenance"
PRICE_WRITE_PERMISSION = "product.price.write"

INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"store_id": {"type": "string"}, "sku": {"type": "string"}},
    "required": ["store_id", "sku"],
}
OUTPUT_SCHEMA: dict[str, object] = {"type": "object", "properties": {"price": {"type": "string"}}}
SCHEMA_HASH = compute_schema_hash(INPUT_SCHEMA, OUTPUT_SCHEMA)


@dataclass(frozen=True)
class CapabilityCatalog:
    """DB 种子行与对应的进程内注册记录、已校验组装。"""

    workspace: Workspace
    user: User
    membership: WorkspaceMembership
    model_config_version: ModelConfigVersion
    agent_record: AgentVersionRecord
    tool_record: ToolVersionRecord
    assembly: RuntimeAssembly


@pytest.fixture()
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个测试一个独立 Session;结束时不提交,回滚以保证测试间隔离。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


@pytest.fixture()
async def capability_catalog(db_session: AsyncSession) -> CapabilityCatalog:
    """种子能力目录行并构造等价的进程内注册表与 RuntimeAssembly。"""
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    user = User(id=uuid.uuid4(), login=f"user-{uuid.uuid4()}", password_hash="test-hash")
    agent = Agent(id=uuid.uuid4(), stable_key=AGENT_KEY, name="商品运营维护 Agent")
    skill = Skill(
        id=uuid.uuid4(),
        scope="GLOBAL",
        stable_key="price-adjustment",
        name="调价技能",
    )
    tool_definition_row = ToolDefinitionRow(
        id=uuid.uuid4(),
        scope="GLOBAL",
        stable_key="update_price",
        name="更新价格",
    )
    db_session.add_all([workspace, user, agent, skill, tool_definition_row])
    await db_session.flush()

    membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=user.id,
        role=MembershipRole.OPERATOR,
    )
    definition = AgentDefinition(
        name="商品运营维护 Agent",
        system_prompt_version="v1",
        required_backend_capabilities=frozenset({"streaming", "tool_calling"}),
        required_skill_keys=frozenset({"price-adjustment"}),
        required_tool_keys=frozenset({"update_price"}),
        required_source_keys=frozenset({"commerce-platform"}),
    )
    agent_version = AgentVersion(
        id=uuid.uuid4(),
        agent_id=agent.id,
        version=1,
        definition_json=definition.model_dump(mode="json"),
        definition_hash="agent-definition-hash",
    )
    skill_version_row = SkillVersion(
        id=uuid.uuid4(),
        workspace_id=None,
        skill_id=skill.id,
        version=1,
        content_json={},
        content_hash="skill-content-hash",
    )
    tool_version_row = ToolVersionRow(
        id=uuid.uuid4(),
        workspace_id=None,
        tool_id=tool_definition_row.id,
        version=1,
        effect="WRITE",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        schema_hash=SCHEMA_HASH,
    )
    model_connection = ModelConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        stable_key=f"model-{uuid.uuid4()}",
        name="test-model",
    )
    db_session.add_all([membership, agent_version, skill_version_row, tool_version_row, model_connection])
    await db_session.flush()

    model_config_version = ModelConfigVersion(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        model_connection_id=model_connection.id,
        version=1,
        backend_type=ModelBackendType.OPENAI,
        base_url="https://api.example.com/v1",
        model="gpt-test",
        non_secret_options={},
        config_hash="model-config-hash",
    )
    source_row = Source(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        stable_key="commerce-platform",
        kind="MCP",
        name="商务平台 MCP",
    )
    db_session.add_all([model_config_version, source_row])
    await db_session.flush()
    source_version_row = SourceVersion(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        source_id=source_row.id,
        version=1,
        config_json={},
        config_hash="source-config-hash",
    )
    db_session.add(source_version_row)
    db_session.add_all(
        MembershipPermission(
            workspace_id=workspace.id,
            membership_id=membership.id,
            permission=permission,
        )
        for permission in ("session.create", "run.read", "product.read", PRICE_WRITE_PERMISSION)
    )
    await db_session.flush()

    agent_rec = AgentVersionRecord(
        stable_key=AGENT_KEY,
        agent_id=agent.id,
        version_id=agent_version.id,
        version=1,
        definition=definition,
    )
    skill_rec = SkillVersionRecord(
        stable_key="price-adjustment",
        version_id=skill_version_row.id,
        version=1,
        workspace_id=None,
        content=SkillDefinition(name="price-adjustment"),
    )
    tool_rec = ToolVersionRecord(
        stable_key="update_price",
        version_id=tool_version_row.id,
        version=1,
        workspace_id=None,
        source_kind="LOCAL",
        effect=ToolEffect.WRITE,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        schema_hash=SCHEMA_HASH,
        required_permissions=frozenset({PRICE_WRITE_PERMISSION}),
    )
    source_rec = SourceVersionRecord(
        stable_key="commerce-platform",
        version_id=source_version_row.id,
        version=1,
        workspace_id=workspace.id,
        kind="MCP",
    )

    agent_registry = AgentRegistry()
    agent_registry.register(agent_rec)
    skill_registry = SkillRegistry()
    skill_registry.register(skill_rec)
    tool_registry = ToolRegistry()
    tool_registry.register(tool_rec)
    tool_registry.bind(
        ToolBinding(
            definition=ToolDefinition(
                stable_key="update_price",
                version=1,
                description="更新价格",
                input_json_schema=INPUT_SCHEMA,
                output_json_schema=OUTPUT_SCHEMA,
                effect=ToolEffect.WRITE,
                required_permissions=frozenset({PRICE_WRITE_PERMISSION}),
                source_kind="LOCAL",
                schema_hash=SCHEMA_HASH,
            ),
            handler=object(),
            pretool_registration=PreToolProfileRegistration(
                factory=_StaticProfileFactory(),
                policy_mode="BUSINESS",
                resource_mode="RESOURCE",
            ),
            execution_profile=ToolExecutionProfile(adapter_key="test-adapter"),
            write_adapter_capabilities=frozenset({"operation", "reconciliation"}),
        )
    )
    source_registry = SourceRegistry()
    source_registry.register(source_rec)
    backend_registry = BackendRegistry()
    backend_registry.register(
        "scripted",
        ScriptedBackend([], capability_keys=frozenset({"streaming", "tool_calling"})),
    )
    assembly = RuntimeAssembly(
        agents=agent_registry,
        skills=skill_registry,
        tools=tool_registry,
        sources=source_registry,
        backends=backend_registry,
        backend_key="scripted",
    )
    assembly.validate()
    return CapabilityCatalog(
        workspace=workspace,
        user=user,
        membership=membership,
        model_config_version=model_config_version,
        agent_record=agent_rec,
        tool_record=tool_rec,
        assembly=assembly,
    )


@pytest.fixture()
def run_factory(db_session: AsyncSession, capability_catalog: CapabilityCatalog) -> RunFactory:
    return RunFactory(
        session=db_session,
        assembly=capability_catalog.assembly,
        workspace_id=capability_catalog.workspace.id,
        actor_id=capability_catalog.user.id,
        membership_id=capability_catalog.membership.id,
        model_config_version_id=capability_catalog.model_config_version.id,
    )


class MembershipPermissionRepository:
    """测试辅助:直接操作 membership_permissions 表(计划原文 revoke 语义)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def revoke(self, actor_id: uuid.UUID, permission: str) -> None:
        """删除该 actor 全部成员身份上的指定动作权限。"""
        membership_ids = select(WorkspaceMembership.id).where(
            WorkspaceMembership.user_id == actor_id
        )
        await self._session.execute(
            delete(MembershipPermission).where(
                MembershipPermission.permission == permission,
                MembershipPermission.membership_id.in_(membership_ids),
            )
        )
        await self._session.flush()


@pytest.fixture()
def membership_repository(db_session: AsyncSession) -> MembershipPermissionRepository:
    return MembershipPermissionRepository(db_session)


@pytest.fixture()
def write_service(
    db_session: AsyncSession, capability_catalog: CapabilityCatalog
) -> WriteAuthorizationService:
    return WriteAuthorizationService(session=db_session, assembly=capability_catalog.assembly)
