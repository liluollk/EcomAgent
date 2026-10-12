"""商品运营能力发布(ADR-002 P0;Provisioning 计划 Task 2)。

发布与装配分离:本模块是唯一写 Agent/Skill/Tool/Model 版本化配置的地方,
按 ``stable_key`` + version 幂等发布并返回精确 Version ID 快照;
``build_product_application`` 只消费该快照,不再持有 scripted 默认值或
配置表写入路径。

依赖方向:``composition.provisioning`` 只依赖 agents 定义与 runtime 持久层/
能力校验,不 import Runtime、AgentLoop 或 ToolExecutor。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.agent import (
    AGENT_KEY,
    AGENT_VERSION,
    PRODUCT_AGENT_DEFINITION,
)
from agents.product_operations.skill import (
    PRODUCT_MAINTENANCE_SKILL,
    SKILL_KEY,
    SKILL_VERSION,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import UPDATE_PRICE_DEFINITION
from runtime.capabilities.skill_activation import (
    LOAD_SKILL_DEFINITION,
    LOAD_SKILL_KEY,
)
from runtime.capabilities.validation import AssemblyError
from runtime.persistence.base import (
    ConfigScope,
    ConfigVersionStatus,
    ModelBackendType,
)
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
    Skill,
    SkillVersion,
)
from runtime.persistence.models.configuration import (
    ToolDefinition as ToolDefinitionRow,
)
from runtime.persistence.models.configuration import (
    ToolVersion as ToolVersionRow,
)


MODEL_CONNECTION_KEY: Final = "product-worker-model"

CAPABILITY_SNAPSHOT_NOT_PUBLISHED: Final = "CAPABILITY_SNAPSHOT_NOT_PUBLISHED"


@dataclass(frozen=True)
class ModelConnectionProvisioningSpec:
    """发布模型连接所需的显式规范;不含任何 scripted 默认值。"""

    stable_key: str
    backend_type: ModelBackendType
    base_url: str
    model: str
    non_secret_options: Mapping[str, Any]


@dataclass(frozen=True)
class ProductOperationsCapabilities:
    """一次发布得到的精确版本 ID 快照(装配只读消费)。"""

    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    tool_definition_ids: Mapping[str, uuid.UUID]
    tool_version_ids: Mapping[str, uuid.UUID]
    model_config_version_id: uuid.UUID

    def as_ids(self) -> dict[str, Any]:
        """兼容既有字典式访问(测试断言与清理使用)。"""
        return {
            "agent_id": self.agent_id,
            "agent_version_id": self.agent_version_id,
            "skill_id": self.skill_id,
            "skill_version_id": self.skill_version_id,
            "tool_definition_ids": dict(self.tool_definition_ids),
            "tool_version_ids": dict(self.tool_version_ids),
            "model_config_version_id": self.model_config_version_id,
        }


def product_tool_definitions() -> tuple[tuple[str, Any], ...]:
    """商品 Tool 与 Runtime 内置 Tool 的确定性注册序列。"""
    return (
        (LOAD_SKILL_KEY, LOAD_SKILL_DEFINITION),
        (SEARCH_PRODUCTS_DEFINITION.stable_key, SEARCH_PRODUCTS_DEFINITION),
        (GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key, GET_PRODUCT_SNAPSHOT_DEFINITION),
        (
            COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            COMPARE_LISTING_PRICES_DEFINITION,
        ),
        (UPDATE_PRICE_DEFINITION.stable_key, UPDATE_PRICE_DEFINITION),
    )


def _json_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProductOperationsProvisioner:
    """幂等发布商品运营能力版本,返回精确 Version ID 快照。

    四个私有发布方法直接接收调用方的 Session,不自行开启事务或创建 Session
    (整个发布是一个事务,任一步失败即整体回滚)。
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def provision(
        self,
        *,
        workspace_id: uuid.UUID,
        model: ModelConnectionProvisioningSpec,
    ) -> ProductOperationsCapabilities:
        session = self._session_factory()
        try:
            async with session.begin():
                agent_id, agent_version_id = await self._provision_agent(session)
                skill_id, skill_version_id = await self._provision_skills(session)
                tool_definition_ids, tool_version_ids = await self._provision_tools(session)
                model_config_version_id = await self._provision_model(
                    session, workspace_id=workspace_id, spec=model
                )
            return ProductOperationsCapabilities(
                agent_id=agent_id,
                agent_version_id=agent_version_id,
                skill_id=skill_id,
                skill_version_id=skill_version_id,
                tool_definition_ids=tool_definition_ids,
                tool_version_ids=tool_version_ids,
                model_config_version_id=model_config_version_id,
            )
        finally:
            await session.close()

    async def _provision_agent(
        self, session: AsyncSession
    ) -> tuple[uuid.UUID, uuid.UUID]:
        agent = (
            await session.scalars(select(Agent).where(Agent.stable_key == AGENT_KEY))
        ).one_or_none()
        if agent is None:
            agent = Agent(
                id=uuid.uuid4(), stable_key=AGENT_KEY, name="ProductMaintenanceAgent"
            )
            session.add(agent)
            await session.flush()
        agent_version = (
            await session.scalars(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent.id,
                    AgentVersion.version == AGENT_VERSION,
                )
            )
        ).one_or_none()
        if agent_version is None:
            definition_json = PRODUCT_AGENT_DEFINITION.model_dump(mode="json")
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=AGENT_VERSION,
                status=ConfigVersionStatus.PUBLISHED,
                definition_json=definition_json,
                definition_hash=_json_hash(definition_json),
                published_at=datetime.now(UTC),
            )
            session.add(agent_version)
            await session.flush()
        elif agent_version.status == ConfigVersionStatus.DRAFT:
            # Built-in reference Agent v1 is released by provisioning; do not
            # leave legacy rows unbindable after the Workspace binding gate.
            agent_version.status = ConfigVersionStatus.PUBLISHED
            agent_version.published_at = datetime.now(UTC)
        return agent.id, agent_version.id

    async def _provision_skills(
        self, session: AsyncSession
    ) -> tuple[uuid.UUID, uuid.UUID]:
        skill = (
            await session.scalars(select(Skill).where(Skill.stable_key == SKILL_KEY))
        ).one_or_none()
        if skill is None:
            skill = Skill(
                id=uuid.uuid4(),
                scope=ConfigScope.GLOBAL,
                workspace_id=None,
                stable_key=SKILL_KEY,
                name=PRODUCT_MAINTENANCE_SKILL.name or SKILL_KEY,
            )
            session.add(skill)
            await session.flush()
        skill_version = (
            await session.scalars(
                select(SkillVersion).where(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.version == SKILL_VERSION,
                )
            )
        ).one_or_none()
        if skill_version is None:
            content_json = PRODUCT_MAINTENANCE_SKILL.model_dump(mode="json")
            skill_version = SkillVersion(
                id=uuid.uuid4(),
                workspace_id=None,
                skill_id=skill.id,
                version=SKILL_VERSION,
                content_json=content_json,
                content_hash=_json_hash(content_json),
            )
            session.add(skill_version)
            await session.flush()
        return skill.id, skill_version.id

    async def _provision_tools(
        self, session: AsyncSession
    ) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
        tool_definition_ids: dict[str, uuid.UUID] = {}
        tool_version_ids: dict[str, uuid.UUID] = {}
        for key, definition in product_tool_definitions():
            definition_row = (
                await session.scalars(
                    select(ToolDefinitionRow).where(
                        ToolDefinitionRow.scope == ConfigScope.GLOBAL,
                        ToolDefinitionRow.stable_key == key,
                    )
                )
            ).one_or_none()
            if definition_row is None:
                definition_row = ToolDefinitionRow(
                    id=uuid.uuid4(),
                    scope=ConfigScope.GLOBAL,
                    workspace_id=None,
                    stable_key=key,
                    name=key,
                )
                session.add(definition_row)
                await session.flush()
            version_row = (
                await session.scalars(
                    select(ToolVersionRow).where(
                        ToolVersionRow.workspace_id.is_(None),
                        ToolVersionRow.tool_id == definition_row.id,
                        ToolVersionRow.version == definition.version,
                    )
                )
            ).one_or_none()
            if version_row is None:
                version_row = ToolVersionRow(
                    id=uuid.uuid4(),
                    workspace_id=None,
                    tool_id=definition_row.id,
                    version=definition.version,
                    effect=definition.effect,
                    input_schema=definition.input_json_schema,
                    output_schema=definition.output_json_schema,
                    schema_hash=definition.schema_hash,
                    implementation_key=key,
                )
                session.add(version_row)
                await session.flush()
            tool_definition_ids[key] = definition_row.id
            tool_version_ids[key] = version_row.id
        return tool_definition_ids, tool_version_ids

    async def _provision_model(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        spec: ModelConnectionProvisioningSpec,
    ) -> uuid.UUID:
        connection = (
            await session.scalars(
                select(ModelConnection).where(
                    ModelConnection.workspace_id == workspace_id,
                    ModelConnection.stable_key == MODEL_CONNECTION_KEY,
                )
            )
        ).one_or_none()
        if connection is None:
            connection = ModelConnection(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                stable_key=MODEL_CONNECTION_KEY,
                name="product scripted model",
            )
            session.add(connection)
            await session.flush()
        model_config_version = (
            await session.scalars(
                select(ModelConfigVersion).where(
                    ModelConfigVersion.model_connection_id == connection.id,
                    ModelConfigVersion.version == 1,
                )
            )
        ).one_or_none()
        if model_config_version is None:
            model_config_version = ModelConfigVersion(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                model_connection_id=connection.id,
                version=1,
                backend_type=spec.backend_type,
                base_url=spec.base_url,
                model=spec.model,
                non_secret_options=dict(spec.non_secret_options),
                config_hash=_json_hash({"model": spec.model}),
            )
            session.add(model_config_version)
            await session.flush()
        return model_config_version.id


async def require_published_capabilities(
    engine: AsyncEngine, capabilities: ProductOperationsCapabilities
) -> None:
    """校验快照指向的版本行均已存在且已发布;否则抛 ``AssemblyError``。

    只读校验:不创建、不修改任何配置行,失败时数据库保持原样。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        agent_version = (
            await session.scalars(
                select(AgentVersion).where(
                    AgentVersion.id == capabilities.agent_version_id
                )
            )
        ).one_or_none()
        if (
            agent_version is None
            or agent_version.status is not ConfigVersionStatus.PUBLISHED
        ):
            raise AssemblyError(
                CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                f"AgentVersion {capabilities.agent_version_id} 未发布",
            )
        skill_version = (
            await session.scalars(
                select(SkillVersion).where(
                    SkillVersion.id == capabilities.skill_version_id
                )
            )
        ).one_or_none()
        if skill_version is None:
            raise AssemblyError(
                CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                f"SkillVersion {capabilities.skill_version_id} 不存在",
            )
        for key, version_id in capabilities.tool_version_ids.items():
            tool_version = (
                await session.scalars(
                    select(ToolVersionRow).where(ToolVersionRow.id == version_id)
                )
            ).one_or_none()
            if tool_version is None:
                raise AssemblyError(
                    CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                    f"Tool {key} 的版本 {version_id} 不存在",
                )
        model_config_version = (
            await session.scalars(
                select(ModelConfigVersion).where(
                    ModelConfigVersion.id == capabilities.model_config_version_id
                )
            )
        ).one_or_none()
        if model_config_version is None:
            raise AssemblyError(
                CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                f"ModelConfigVersion {capabilities.model_config_version_id} 不存在",
            )
    finally:
        await session.close()


__all__ = [
    "CAPABILITY_SNAPSHOT_NOT_PUBLISHED",
    "MODEL_CONNECTION_KEY",
    "ModelConnectionProvisioningSpec",
    "ProductOperationsCapabilities",
    "ProductOperationsProvisioner",
    "product_tool_definitions",
    "require_published_capabilities",
]
