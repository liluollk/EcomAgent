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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.agent import (
    AGENT_KEY,
    AGENT_VERSION,
    LEGACY_AGENT_KEY,
    LEGACY_AGENT_VERSION,
    LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION,
    PRODUCT_AGENT_DEFINITION,
)
from agents.product_operations.catalog.tools import SEARCH_PRODUCTS_DEFINITION
from agents.product_operations.inventory.tools import (
    ADJUST_INVENTORY_QUANTITY_DEFINITION,
    GET_INVENTORY_LEVEL_DEFINITION,
)
from agents.product_operations.listing.tools import (
    GET_LISTING_DEFINITION,
    PUBLISH_LISTING_DEFINITION,
    UNPUBLISH_LISTING_DEFINITION,
)
from agents.product_operations.policy.models import PricePolicyConfig
from agents.product_operations.pricing.configuration import (
    PRICING_POLICY_CONFIG_INVALID,
    PRICING_POLICY_NOT_PUBLISHED,
    PRICING_POLICY_STABLE_KEY,
    InvalidPricingPolicyConfig,
    PublishedPricingPolicy,
    canonical_pricing_policy_payload,
    parse_pricing_policy_config,
    pricing_policy_config_hash,
)
from agents.product_operations.pricing.tools import (
    COMPARE_LISTING_PRICES_DEFINITION,
    SET_LISTING_PRICE_DEFINITION,
)
from agents.product_operations.skill import (
    LEGACY_PRODUCT_MAINTENANCE_SKILL,
    LEGACY_SKILL_VERSION,
    PRODUCT_MAINTENANCE_SKILL,
    SKILL_KEY,
    SKILL_VERSION,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION as LEGACY_COMPARE_LISTING_PRICES_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    GET_PRODUCT_SNAPSHOT_DEFINITION as LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    SEARCH_PRODUCTS_DEFINITION as LEGACY_SEARCH_PRODUCTS_DEFINITION,
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
    BusinessPolicy,
    BusinessPolicyVersion,
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
    """一次发布得到的精确版本 ID 快照(装配只读消费)。

    ``pricing_policy_version_id`` / ``pricing_policy_config_hash`` 指向**已发布**的
    价格策略行:装配期据此读取唯一一份规则,而不是从常量或调用参数注入。缺少它们
    就意味着"这次发布没有发布价格策略",装配必须失败闭合。
    """

    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    tool_definition_ids: Mapping[str, uuid.UUID]
    tool_version_ids: Mapping[str, uuid.UUID]
    model_config_version_id: uuid.UUID
    pricing_policy_version_id: uuid.UUID
    pricing_policy_config_hash: str
    compatibility_agent_version_ids: tuple[uuid.UUID, ...] = ()
    compatibility_skill_version_ids: tuple[uuid.UUID, ...] = ()
    compatibility_tool_version_ids: Mapping[str, tuple[uuid.UUID, ...]] = field(
        default_factory=dict
    )
    compatibility_agent_ids: Mapping[uuid.UUID, uuid.UUID] = field(
        default_factory=dict
    )

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
            "pricing_policy_version_id": self.pricing_policy_version_id,
            "pricing_policy_config_hash": self.pricing_policy_config_hash,
            "compatibility_agent_version_ids": list(
                self.compatibility_agent_version_ids
            ),
            "compatibility_skill_version_ids": list(
                self.compatibility_skill_version_ids
            ),
            "compatibility_tool_version_ids": {
                key: list(version_ids)
                for key, version_ids in self.compatibility_tool_version_ids.items()
            },
            "compatibility_agent_ids": {
                str(version_id): agent_id
                for version_id, agent_id in self.compatibility_agent_ids.items()
            },
        }


def product_tool_definitions() -> tuple[tuple[str, Any], ...]:
    """新 Run 使用的 P1 Tool 与 Runtime 内置 Tool 确定性注册序列。"""
    return (
        (LOAD_SKILL_KEY, LOAD_SKILL_DEFINITION),
        (SEARCH_PRODUCTS_DEFINITION.stable_key, SEARCH_PRODUCTS_DEFINITION),
        (GET_LISTING_DEFINITION.stable_key, GET_LISTING_DEFINITION),
        (
            COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            COMPARE_LISTING_PRICES_DEFINITION,
        ),
        (SET_LISTING_PRICE_DEFINITION.stable_key, SET_LISTING_PRICE_DEFINITION),
        (GET_INVENTORY_LEVEL_DEFINITION.stable_key, GET_INVENTORY_LEVEL_DEFINITION),
        (
            ADJUST_INVENTORY_QUANTITY_DEFINITION.stable_key,
            ADJUST_INVENTORY_QUANTITY_DEFINITION,
        ),
        (PUBLISH_LISTING_DEFINITION.stable_key, PUBLISH_LISTING_DEFINITION),
        (UNPUBLISH_LISTING_DEFINITION.stable_key, UNPUBLISH_LISTING_DEFINITION),
    )


def legacy_product_tool_definitions() -> tuple[tuple[str, Any], ...]:
    """仅供历史 Run 恢复的第一版业务 Tool 版本。"""
    return (
        (
            LEGACY_SEARCH_PRODUCTS_DEFINITION.stable_key,
            LEGACY_SEARCH_PRODUCTS_DEFINITION,
        ),
        (
            LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
            LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION,
        ),
        (
            LEGACY_COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            LEGACY_COMPARE_LISTING_PRICES_DEFINITION,
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
        pricing_policy: PricePolicyConfig,
    ) -> ProductOperationsCapabilities:
        """发布一次完整能力快照。

        ``pricing_policy`` 是**显式注入**的规则配置:发布方(测试夹具、Eval、E2E)
        提供它,正式装配路径不再接受价格配置参数,只消费这里发布的精确版本。
        """
        session = self._session_factory()
        try:
            async with session.begin():
                agent_id, agent_version_id = await self._provision_agent(
                    session,
                    stable_key=AGENT_KEY,
                    version=AGENT_VERSION,
                    name="ProductOperationsAgent",
                    definition=PRODUCT_AGENT_DEFINITION,
                )
                legacy_agent_id, legacy_agent_version_id = await self._provision_agent(
                    session,
                    stable_key=LEGACY_AGENT_KEY,
                    version=LEGACY_AGENT_VERSION,
                    name="ProductMaintenanceAgent",
                    definition=LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION,
                )
                skill_id, skill_version_id = await self._provision_skill(
                    session,
                    version=SKILL_VERSION,
                    definition=PRODUCT_MAINTENANCE_SKILL,
                )
                _, legacy_skill_version_id = await self._provision_skill(
                    session,
                    version=LEGACY_SKILL_VERSION,
                    definition=LEGACY_PRODUCT_MAINTENANCE_SKILL,
                )
                tool_definition_ids, tool_version_ids = await self._provision_tools(
                    session, definitions=product_tool_definitions()
                )
                _, legacy_tool_version_ids = await self._provision_tools(
                    session, definitions=legacy_product_tool_definitions()
                )
                model_config_version_id = await self._provision_model(
                    session, workspace_id=workspace_id, spec=model
                )
                published_policy = await self._provision_pricing_policy(
                    session, workspace_id=workspace_id, config=pricing_policy
                )
            return ProductOperationsCapabilities(
                agent_id=agent_id,
                agent_version_id=agent_version_id,
                skill_id=skill_id,
                skill_version_id=skill_version_id,
                tool_definition_ids=tool_definition_ids,
                tool_version_ids=tool_version_ids,
                model_config_version_id=model_config_version_id,
                pricing_policy_version_id=published_policy.version_id,
                pricing_policy_config_hash=published_policy.config_hash,
                compatibility_agent_version_ids=(legacy_agent_version_id,),
                compatibility_agent_ids={
                    legacy_agent_version_id: legacy_agent_id
                },
                compatibility_skill_version_ids=(legacy_skill_version_id,),
                compatibility_tool_version_ids={
                    key: (version_id,)
                    for key, version_id in legacy_tool_version_ids.items()
                },
            )
        finally:
            await session.close()

    async def _provision_agent(
        self,
        session: AsyncSession,
        *,
        stable_key: str,
        version: int,
        name: str,
        definition: Any,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        agent = (
            await session.scalars(select(Agent).where(Agent.stable_key == stable_key))
        ).one_or_none()
        if agent is None:
            agent = Agent(id=uuid.uuid4(), stable_key=stable_key, name=name)
            session.add(agent)
            await session.flush()
        agent_version = (
            await session.scalars(
                select(AgentVersion).where(
                    AgentVersion.agent_id == agent.id,
                    AgentVersion.version == version,
                )
            )
        ).one_or_none()
        if agent_version is None:
            definition_json = definition.model_dump(mode="json")
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=version,
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

    async def _provision_skill(
        self,
        session: AsyncSession,
        *,
        version: int,
        definition: Any,
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
                name=definition.name or SKILL_KEY,
            )
            session.add(skill)
            await session.flush()
        skill_version = (
            await session.scalars(
                select(SkillVersion).where(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.version == version,
                )
            )
        ).one_or_none()
        if skill_version is None:
            content_json = definition.model_dump(mode="json")
            skill_version = SkillVersion(
                id=uuid.uuid4(),
                workspace_id=None,
                skill_id=skill.id,
                version=version,
                content_json=content_json,
                content_hash=_json_hash(content_json),
            )
            session.add(skill_version)
            await session.flush()
        return skill.id, skill_version.id

    async def _provision_tools(
        self,
        session: AsyncSession,
        *,
        definitions: tuple[tuple[str, Any], ...],
    ) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
        tool_definition_ids: dict[str, uuid.UUID] = {}
        tool_version_ids: dict[str, uuid.UUID] = {}
        for key, definition in definitions:
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

    async def _provision_pricing_policy(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        config: PricePolicyConfig,
    ) -> PublishedPricingPolicy:
        """把价格策略发布到 ``BusinessPolicy`` / ``BusinessPolicyVersion``。

        幂等口径是**规范化载荷**而非对象相等:同一份配置产生逐字节相同的载荷与
        hash,于是复用已发布行;配置任一项变化则 hash 变化,发布一个新版本(旧版本
        保留,便于历史 Run 解释自己当时读的是哪一版规则)。
        """
        payload = canonical_pricing_policy_payload(config)
        config_hash = pricing_policy_config_hash(payload)
        policy = (
            await session.scalars(
                select(BusinessPolicy).where(
                    BusinessPolicy.workspace_id == workspace_id,
                    BusinessPolicy.stable_key == PRICING_POLICY_STABLE_KEY,
                )
            )
        ).one_or_none()
        if policy is None:
            policy = BusinessPolicy(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                stable_key=PRICING_POLICY_STABLE_KEY,
                name="Product pricing policy",
            )
            session.add(policy)
            await session.flush()
        existing = list(
            await session.scalars(
                select(BusinessPolicyVersion)
                .where(
                    BusinessPolicyVersion.workspace_id == workspace_id,
                    BusinessPolicyVersion.business_policy_id == policy.id,
                )
                .order_by(BusinessPolicyVersion.version)
            )
        )
        for row in existing:
            if row.config_hash == config_hash and row.config_json == payload:
                return PublishedPricingPolicy(
                    version_id=row.id,
                    version=row.version,
                    config=config,
                    config_hash=config_hash,
                )
        version = 1 + max((row.version for row in existing), default=0)
        row = BusinessPolicyVersion(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            business_policy_id=policy.id,
            version=version,
            config_json=payload,
            config_hash=config_hash,
            status=ConfigVersionStatus.PUBLISHED,
            published_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return PublishedPricingPolicy(
            version_id=row.id,
            version=row.version,
            config=config,
            config_hash=config_hash,
        )


async def load_published_pricing_policy(
    engine: AsyncEngine,
    *,
    workspace_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_config_hash: str,
) -> PublishedPricingPolicy:
    """读取并校验已发布的价格策略;任何不一致都以 ``AssemblyError`` fail closed。

    五项校验缺一不可,因为每一项都对应一类真实错误装配:

    - 行存在且属于**本 Workspace**(跨 Workspace 读策略等于用别人的规则);
    - 策略目录的 ``stable_key`` 是 ``product-pricing``(防错指到别的业务规则);
    - 版本状态为 ``PUBLISHED``(草稿不得进入执行路径);
    - 行 ``config_hash`` 等于能力快照里的 hash(防"发布后又改了行");
    - 载荷能严格还原成 :class:`PricePolicyConfig`,且重新计算的 hash 与行一致
      (防脏数据写着合法 hash、内容却不是同一份配置)。

    只读:不创建、不修改任何行。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        row = (
            await session.execute(
                select(BusinessPolicyVersion, BusinessPolicy)
                .join(
                    BusinessPolicy,
                    (BusinessPolicy.workspace_id == BusinessPolicyVersion.workspace_id)
                    & (BusinessPolicy.id == BusinessPolicyVersion.business_policy_id),
                )
                .where(
                    BusinessPolicyVersion.id == version_id,
                    BusinessPolicyVersion.workspace_id == workspace_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise AssemblyError(
                PRICING_POLICY_NOT_PUBLISHED,
                f"BusinessPolicyVersion {version_id} 不存在于该 Workspace",
            )
        version_row, policy_row = row
        if policy_row.stable_key != PRICING_POLICY_STABLE_KEY:
            raise AssemblyError(
                PRICING_POLICY_NOT_PUBLISHED,
                f"策略 stable_key 不是 {PRICING_POLICY_STABLE_KEY}: {policy_row.stable_key!r}",
            )
        if version_row.status is not ConfigVersionStatus.PUBLISHED:
            raise AssemblyError(
                PRICING_POLICY_NOT_PUBLISHED,
                f"价格策略版本 {version_id} 状态为 {version_row.status.value}",
            )
        if version_row.config_hash != expected_config_hash:
            raise AssemblyError(
                PRICING_POLICY_NOT_PUBLISHED,
                f"价格策略版本 {version_id} 的 config_hash 与能力快照不一致",
            )
        try:
            config = parse_pricing_policy_config(version_row.config_json)
        except InvalidPricingPolicyConfig as error:
            raise AssemblyError(PRICING_POLICY_CONFIG_INVALID, error.detail) from error
        recomputed = pricing_policy_config_hash(
            canonical_pricing_policy_payload(config)
        )
        if recomputed != version_row.config_hash:
            raise AssemblyError(
                PRICING_POLICY_CONFIG_INVALID,
                f"价格策略版本 {version_id} 的载荷与其 config_hash 不自洽",
            )
        return PublishedPricingPolicy(
            version_id=version_row.id,
            version=version_row.version,
            config=config,
            config_hash=recomputed,
        )
    finally:
        await session.close()


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
        for version_id in capabilities.compatibility_agent_version_ids:
            compatibility_agent = (
                await session.scalars(
                    select(AgentVersion).where(AgentVersion.id == version_id)
                )
            ).one_or_none()
            if (
                compatibility_agent is None
                or compatibility_agent.status is not ConfigVersionStatus.PUBLISHED
            ):
                raise AssemblyError(
                    CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                    f"兼容 AgentVersion {version_id} 未发布",
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
        for version_id in capabilities.compatibility_skill_version_ids:
            compatibility_skill = (
                await session.scalars(
                    select(SkillVersion).where(SkillVersion.id == version_id)
                )
            ).one_or_none()
            if compatibility_skill is None:
                raise AssemblyError(
                    CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                    f"兼容 SkillVersion {version_id} 不存在",
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
        for key, version_ids in capabilities.compatibility_tool_version_ids.items():
            for version_id in version_ids:
                tool_version = (
                    await session.scalars(
                        select(ToolVersionRow).where(ToolVersionRow.id == version_id)
                    )
                ).one_or_none()
                if tool_version is None:
                    raise AssemblyError(
                        CAPABILITY_SNAPSHOT_NOT_PUBLISHED,
                        f"兼容 Tool {key} 的版本 {version_id} 不存在",
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
        # 在会话内取走标量:关闭后不再依赖 ORM 实例的惰性加载。
        pricing_workspace_id = model_config_version.workspace_id
    finally:
        await session.close()
    # 价格策略必须同样处于已发布可读状态:能力快照含"哪一版规则",装配期必须能
    # 真正读到它,否则等于这次发布没有可执行的价格规则。
    await load_published_pricing_policy(
        engine,
        workspace_id=pricing_workspace_id,
        version_id=capabilities.pricing_policy_version_id,
        expected_config_hash=capabilities.pricing_policy_config_hash,
    )


__all__ = [
    "CAPABILITY_SNAPSHOT_NOT_PUBLISHED",
    "MODEL_CONNECTION_KEY",
    "ModelConnectionProvisioningSpec",
    "ProductOperationsCapabilities",
    "ProductOperationsProvisioner",
    "legacy_product_tool_definitions",
    "load_published_pricing_policy",
    "product_tool_definitions",
    "require_published_capabilities",
]
