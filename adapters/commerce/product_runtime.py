"""商品域运行面组合核心与门面(计划 03 Task 6;计划 05 Task 2 下沉)。

``build_product_runtime`` 是 ProductMaintenance Agent 的组装核心:

- 启动期把 Agent/Skill/Tool/Model 版本写入数据库并注册进程内 Registry,
  执行 :class:`~runtime.capabilities.assembly.RuntimeAssembly` 启动校验;
- 组装 PreToolUse 管线工厂(注入商品域各阶段实现:输入归一化、参数
  Provenance、InputRequirement、店铺资源解析、价格 Policy、执行模式与
  确认校验)、:class:`~runtime.orchestration.context.ContextAssembler`、
  读写 Tool Handler 与 :class:`~runtime.orchestration.runtime_host.RuntimeHost`;
- 平台桥接:把 :class:`~agents.product_operations.tools.update_price.UpdatePriceGateway`
  实现到 :class:`~adapters.commerce.mock_adapter.MockCommerceAdapter`。

模块归属:本组合核心同时被 apps/worker(生产组合根)与 eval(Eval Runner,
计划 05 Task 2)复用,而 eval 不得 import apps(方向:eval → runtime/
agents/adapters)。本模块原本位于 apps/worker/assembly.py;由于 runtime 不
import agents/adapters、agents 不 import adapters(架构文档 §2),而
adapters 已依赖 agents(:mod:`adapters.commerce.mock_adapter` 先例),故
下沉到本模块作为单一事实源;``apps/worker/assembly.py`` 保留为兼容导出。

``ProductRuntime`` 门面供驱动方(Worker、测试、CLI、Eval)使用:``start``
包装 RunFactory → ADVANCE_RUN Job → RuntimeHost Worker 推进;``answer`` /
``confirm`` 用 Input/Confirmation 服务处理挂起请求后恢复推进;``recover``
对响应丢失的写操作调度 reconcile 回查后恢复推进。Worker 进程按 Run 亲和
持有已回答字段的内存视图(真实部署中应由输入请求持久化重建)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.protocol import ExecutionReceipt as AdapterExecutionReceipt
from adapters.commerce.protocol import UpdatePriceCommand as AdapterPriceCommand
from agents.product_operations.agent import AGENT_KEY, PRODUCT_AGENT_DEFINITION
from agents.product_operations.input.requirements import (
    ProductInputRequirementResolver,
    ProductPreToolInputRequirementResolver,
    ProductPreToolNormalizer,
    ProductPreToolProvenanceResolver,
)
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.policy.models import (
    PricePolicyConfig,
    PricePolicyDecision,
    PriceWriteRequest,
)
from agents.product_operations.policy.price_policy import (
    LISTING_NOT_FOUND as POLICY_LISTING_NOT_FOUND,
)
from agents.product_operations.policy.price_policy import (
    LISTING_VERSION_CONFLICT as POLICY_LISTING_VERSION_CONFLICT,
)
from agents.product_operations.policy.price_policy import PricePolicy
from agents.product_operations.policy.resources import StoreResourceResolver
from agents.product_operations.prompt import PRODUCT_AGENT_PROMPT
from agents.product_operations.skill import (
    PRODUCT_MAINTENANCE_SKILL,
    PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS,
    SKILL_KEY,
)
from agents.product_operations.tools.compare_listing_prices import (
    COMPARE_LISTING_PRICES_DEFINITION,
    compare_listing_prices,
)
from agents.product_operations.tools.definitions import (
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.get_product_snapshot import get_product_snapshot
from agents.product_operations.tools.search_products import search_products
from agents.product_operations.tools.update_price import (
    STABLE_KEY_UPDATE_PRICE,
    UPDATE_PRICE_DEFINITION,
    UpdatePriceToolHandler,
    UpdatePriceWriteCommand,
    UpdatePriceWriteReceipt,
)
from runtime.access.actor import ActorContext, ActorContextBuilder
from runtime.access.authorization import Authorizer
from runtime.backend.protocol import (
    AgentBackend,
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)
from runtime.capabilities.assembly import RuntimeAssembly
from runtime.capabilities.registry import (
    AgentRegistry,
    AgentVersionRecord,
    BackendRegistry,
    SkillRegistry,
    SkillVersionRecord,
    SourceRegistry,
    ToolBinding,
    ToolRegistry,
    ToolVersionRecord,
)
from runtime.capabilities.run_factory import RunFactory
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.pretool.decisions import (
    STAGE_ARGUMENT_PROVENANCE,
    PolicyDecision,
    PreToolDecision,
    SchemaDiagnostic,
)
from runtime.execution.pretool.pipeline import (
    NamedStage,
    PreToolContext,
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.reconciliation import ReconciliationService
from runtime.execution.run_service import RunService
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.execution.tool_result_processor import ToolExecutionReceipt
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interaction.input_service import InputService
from runtime.interfaces.state import RunStatus
from runtime.interfaces.tools import ToolEffect as ToolEffectKind
from runtime.jobs.lease_service import Lease, LeaseOutcome, LeaseService
from runtime.orchestration.agent_loop import ToolCallEvaluator, ToolPin
from runtime.orchestration.context import ContextAssembler
from runtime.orchestration.limits import RunLimits
from runtime.orchestration.messages import MessageStore
from runtime.orchestration.runtime_host import RuntimeHost
from runtime.orchestration.summarization import ConversationSummarizer
from runtime.orchestration.token_estimator import HeuristicTokenEstimator
from runtime.persistence.base import (
    ConfigVersionStatus,
    ConfigScope,
    ConfirmationRequestStatus,
    InputRequestStatus,
    MessageRole,
    ModelBackendType,
    RunJobKind,
    ToolEffect,
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
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
)
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.runs import RunRepository


ADAPTER_KEY: Final = "mock-commerce"
BACKEND_KEY: Final = "product-scripted"
MODEL_CONNECTION_KEY: Final = "product-worker-model"

_RUN_RESUMED_EVENT: Final = "RUN_RESUMED"
_OUTPUT_SCHEMA: Final[dict[str, Any]] = {"type": "object"}
_WRITE_ADAPTER_CAPABILITIES: Final[frozenset[str]] = frozenset({"operation", "reconciliation"})

_LIMITS: Final = RunLimits(
    max_model_rounds=8,
    max_tool_calls=8,
    tool_timeout_seconds=60,
    run_deadline_seconds=3600,
    backend_retries=1,
    reconcile_budget=3,
)

_MAX_RECONCILE_ROUNDS: Final = 5


# ---------------------------------------------------------------------------
# 启动期组装:能力版本落库 + 进程内 Registry
# ---------------------------------------------------------------------------


def _json_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tool_definitions() -> tuple[tuple[str, Any], ...]:
    """四个商品 Tool 的 (stable_key, ToolDefinition) 确定性序列。"""
    return (
        (SEARCH_PRODUCTS_DEFINITION.stable_key, SEARCH_PRODUCTS_DEFINITION),
        (GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key, GET_PRODUCT_SNAPSHOT_DEFINITION),
        (COMPARE_LISTING_PRICES_DEFINITION.stable_key, COMPARE_LISTING_PRICES_DEFINITION),
        (UPDATE_PRICE_DEFINITION.stable_key, UPDATE_PRICE_DEFINITION),
    )


async def _bootstrap_capabilities(
    engine: AsyncEngine, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """把 Agent/Skill/Tool/Model 版本写入数据库(幂等:已存在即复用)。

    返回组合根注册所需的精确 Version ID;进程内 Registry 与数据库行共用
    同一 Version ID,保证 Run 快照(run_tool_versions 等)可解析。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
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
                        AgentVersion.agent_id == agent.id, AgentVersion.version == 1
                    )
                )
            ).one_or_none()
            if agent_version is None:
                definition_json = PRODUCT_AGENT_DEFINITION.model_dump(mode="json")
                agent_version = AgentVersion(
                    id=uuid.uuid4(),
                    agent_id=agent.id,
                    version=1,
                    status=ConfigVersionStatus.PUBLISHED,
                    definition_json=definition_json,
                    definition_hash=_json_hash(definition_json),
                    published_at=datetime.now(UTC),
                )
                session.add(agent_version)
                await session.flush()
            elif agent_version.status == ConfigVersionStatus.DRAFT:
                # Built-in reference Agent v1 is released by this bootstrap; do not
                # leave legacy rows unbindable after the Workspace binding gate.
                agent_version.status = ConfigVersionStatus.PUBLISHED
                agent_version.published_at = datetime.now(UTC)

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
                        SkillVersion.skill_id == skill.id, SkillVersion.version == 1
                    )
                )
            ).one_or_none()
            if skill_version is None:
                content_json = PRODUCT_MAINTENANCE_SKILL.model_dump(mode="json")
                skill_version = SkillVersion(
                    id=uuid.uuid4(),
                    workspace_id=None,
                    skill_id=skill.id,
                    version=1,
                    content_json=content_json,
                    content_hash=_json_hash(content_json),
                )
                session.add(skill_version)
                await session.flush()

            tool_definition_ids: dict[str, uuid.UUID] = {}
            tool_version_ids: dict[str, uuid.UUID] = {}
            for key, definition in _tool_definitions():
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
                    backend_type=ModelBackendType.OPENAI,
                    base_url="https://scripted.model.invalid/v1",
                    model="product-scripted",
                    non_secret_options={},
                    config_hash=_json_hash({"model": "product-scripted"}),
                )
                session.add(model_config_version)
                await session.flush()

            return {
                "agent_id": agent.id,
                "agent_version_id": agent_version.id,
                "skill_id": skill.id,
                "skill_version_id": skill_version.id,
                "tool_definition_ids": tool_definition_ids,
                "tool_version_ids": tool_version_ids,
                "model_config_version_id": model_config_version.id,
            }
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# 进程内协作对象
# ---------------------------------------------------------------------------


class _RegistryToolPins:
    """基于 ToolRegistry 的 ToolPin 解析(Run 钉住版本即注册版本)。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._by_key: dict[str, ToolPin] = {}
        self._by_id: dict[uuid.UUID, ToolPin] = {}
        for record in registry.records():
            if record.effect is None:
                continue
            pin = ToolPin(
                stable_key=record.stable_key,
                version=record.version,
                version_id=record.version_id,
                effect=ToolEffect(record.effect.value),
            )
            self._by_key[record.stable_key] = pin
            self._by_id[record.version_id] = pin

    def by_stable_key(self, stable_key: str) -> ToolPin | None:
        return self._by_key.get(stable_key)

    def by_version_id(self, version_id: uuid.UUID) -> ToolPin | None:
        return self._by_id.get(version_id)


@dataclass(frozen=True)
class _ScopeContext:
    """一次推进的运行上下文(pipeline_factory 每次 advance 取一次)。"""

    workspace_id: uuid.UUID
    run_id: uuid.UUID
    actor_id: uuid.UUID
    answered: Mapping[str, str]


class _ScopeHolder:
    """进程内当前推进上下文;Worker 单线程顺序推进,begin/end 成对出现。"""

    def __init__(self) -> None:
        self._current: _ScopeContext | None = None

    def begin(self, context: _ScopeContext) -> None:
        self._current = context

    def end(self) -> None:
        self._current = None

    def require(self) -> _ScopeContext:
        if self._current is None:
            raise RuntimeError("no advance scope is active; pipeline factory misuse")
        return self._current


class _NoopSummaryBackend:
    """Summary 后备模型(默认组装无输入预算,不会触发)。"""

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(capability_keys=frozenset({"streaming"}))

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        del request, cancellation
        yield BackendCompleted(finish_reason="stop")


class _ProductPipelineFactory:
    """每次 advance 构造绑定当前 Run Actor 的 PreToolUse 管线。

    ``_version_bindings`` 记录 update_price ToolCall 首次通过 Business Policy
    时的 Listing 版本(架构文档 §6 第 7 条"确认后 Listing Version 未失效")。
    确认后恢复推进时重评:版本已变化即 LISTING_VERSION_CONFLICT。绑定表为
    组合根进程内状态,与 Worker 进程按 Run 亲和的内存视图同一生命周期。
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        tool_registry: ToolRegistry,
        price_config: PricePolicyConfig,
        scope: _ScopeHolder,
    ) -> None:
        self._engine = engine
        self._tool_registry = tool_registry
        self._price_config = price_config
        self._scope = scope
        self._version_bindings: dict[uuid.UUID, int] = {}

    def __call__(self) -> ToolCallEvaluator:
        return _ScopedProductPipeline(
            engine=self._engine,
            context=self._scope.require(),
            tool_registry=self._tool_registry,
            price_config=self._price_config,
            version_bindings=self._version_bindings,
        )


class _ScopedProductPipeline:
    """单次推进的 PreToolUse 管线;评估结束后关闭自有读取 Session。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        context: _ScopeContext,
        tool_registry: ToolRegistry,
        price_config: PricePolicyConfig,
        version_bindings: dict[uuid.UUID, int],
    ) -> None:
        self._engine = engine
        self._context = context
        self._tool_registry = tool_registry
        self._price_config = price_config
        self._version_bindings: dict[uuid.UUID, int] = version_bindings

    async def evaluate(self, tool_call: ToolCallRequest) -> PreToolDecision:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            actor = await ActorContextBuilder(session).build(
                actor_id=self._context.actor_id,
                workspace_id=self._context.workspace_id,
                request_id=f"pretool-{tool_call.tool_call_id}",
            )
            user_message = await _current_user_message(
                session, self._context.workspace_id, self._context.run_id
            )
            requirement_resolver = ProductInputRequirementResolver(session)
            dependencies = PreToolDependencies(
                tool_registry=RuntimeToolRegistry(self._tool_registry),
                schema_validator=RuntimeSchemaValidator(),
                authorizer=Authorizer(),
                input_normalizer=ProductPreToolNormalizer(),
                argument_provenance_resolver=ProductPreToolProvenanceResolver(
                    requirement_resolver,
                    user_message=user_message,
                    actor=actor,
                    answered=self._context.answered,
                ),
                input_requirement_resolver=ProductPreToolInputRequirementResolver(
                    requirement_resolver, actor=actor
                ),
                authorization_resource_resolver=StoreResourceResolver(),
                business_policy=_PriceBusinessPolicy(
                    session=session,
                    config=self._price_config,
                    actor=actor,
                    tool_call_id=tool_call.tool_call_id,
                    version_bindings=self._version_bindings,
                ),
                accepted_confirmation_lookup=_AcceptedConfirmationLookup(
                    session=session, workspace_id=self._context.workspace_id
                ),
            )
            pipeline = PreToolUsePipeline(
                actor=actor, stages=_product_stages(dependencies)
            )
            return await pipeline.evaluate(tool_call)
        finally:
            await session.close()


_UNTRUSTED_SOURCE: Final = "UNTRUSTED"
_PROVENANCE_GUARD_FIELDS: Final[tuple[str, ...]] = ("store_id", "sku")
_GUARD_DIAGNOSTIC_MESSAGE: Final = "key arguments lack trusted user or authoritative evidence"


def _product_stages(dependencies: PreToolDependencies) -> tuple[NamedStage, ...]:
    """默认 10 阶段管线 + 证据守卫(阶段顺序不变)。

    架构文档 §5:"任一关键参数缺失、歧义或来源不可信时,返回
    InputRequirement,不进入 Access Control 之后的写执行阶段。"缺失字段由
    Schema 校验诊断覆盖;守卫补充覆盖"模型提供了值但无可信证据"的猜测
    参数(store_id/sku),把它们并入 SchemaDiagnostic,交由既有
    input_requirement 阶段转成可回答的 Input Request。target_price 不在
    守卫范围:合法金额总是 DETERMINISTIC_DERIVED,非法金额属校验诊断
    (架构文档 §5),不产生 Input Request。
    """
    stages = list(default_stages(dependencies))
    return tuple(
        _provenance_guard_stage(stage)
        if stage.name == STAGE_ARGUMENT_PROVENANCE
        else stage
        for stage in stages
    )


def _provenance_guard_stage(wrapped: NamedStage) -> NamedStage:
    """argument_provenance 阶段守卫:不可信的已提供关键参数 → 校验诊断。"""
    original = wrapped.handler

    async def handler(context: PreToolContext) -> PreToolDecision | None:
        decision = await original(context)
        if decision is not None:
            return decision
        provenance = context.provenance
        if provenance is None:
            return None
        arguments = context.normalized_arguments or {}
        untrusted = tuple(
            field
            for field in _PROVENANCE_GUARD_FIELDS
            if field in arguments
            and provenance.field_sources.get(field) == _UNTRUSTED_SOURCE
        )
        if not untrusted:
            return None
        diagnostic = context.schema_diagnostic
        merged_paths = tuple(
            dict.fromkeys(
                (*(diagnostic.field_paths if diagnostic is not None else ()), *untrusted)
            )
        )
        context.schema_diagnostic = SchemaDiagnostic(
            field_paths=merged_paths,
            message=_GUARD_DIAGNOSTIC_MESSAGE,
        )
        return None

    return NamedStage(name=wrapped.name, handler=handler)


class _PriceBusinessPolicy:
    """BusinessPolicy 注入适配:update_price 走价格 Policy,其余 Tool 放行。

    确认前后一致性:首次评估(进入确认前)把当前 Listing 版本绑定到
    ToolCall;确认后恢复推进的重评先比对绑定版本,已变化即以
    ``LISTING_VERSION_CONFLICT`` 拒绝(架构文档 §6 第 7 条;PricePolicy
    自身的乐观并发校验仍照常执行)。
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        config: PricePolicyConfig,
        actor: ActorContext,
        tool_call_id: uuid.UUID,
        version_bindings: dict[uuid.UUID, int],
    ) -> None:
        self._session = session
        self._config = config
        self._actor = actor
        self._tool_call_id = tool_call_id
        self._version_bindings = version_bindings

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        del resources  # 资源范围已在 Access Control 阶段校验。
        if stable_key != STABLE_KEY_UPDATE_PRICE:
            return PolicyDecision(allowed=True)
        decision_or_none = self._parse(arguments)
        if decision_or_none is not None:
            return decision_or_none
        store_id, sku, price = self._arguments_of(arguments)
        listing = await _resolve_listing(self._session, self._actor.workspace_id, store_id, sku)
        if listing is None:
            return self._deny(
                POLICY_LISTING_NOT_FOUND,
                "listing does not exist in this workspace",
                expected_new_price=price,
            )
        stale = self._check_confirmation_version(listing.version)
        if stale is not None:
            return self._deny(
                POLICY_LISTING_VERSION_CONFLICT,
                "listing version has changed since confirmation",
                expected_new_price=price,
            )
        request = PriceWriteRequest(
            workspace_id=self._actor.workspace_id,
            store_id=store_id,
            target_price=price,
            expected_version=listing.version,
            actor=self._actor,
            sku=sku,
        )
        return await PricePolicy(self._session, self._config).evaluate(request)

    def _check_confirmation_version(self, current_version: int) -> int | None:
        """绑定/比对确认时 Listing 版本;返回 None 表示可继续,否则返回当前版本。

        首次评估(尚无绑定)记录当前版本并放行;已有绑定且与当前版本不一致
        表示确认后 Listing 已被并发修改。
        """
        bound = self._version_bindings.get(self._tool_call_id)
        if bound is None:
            self._version_bindings[self._tool_call_id] = current_version
            return None
        return current_version if bound != current_version else None

    def _arguments_of(
        self, arguments: Mapping[str, Any]
    ) -> tuple[uuid.UUID, str, Decimal]:
        """前置条件:``_parse`` 已保证参数形状合法。"""
        return (
            uuid.UUID(str(arguments["store_id"])),
            str(arguments["sku"]),
            cast_decimal(arguments["target_price"]),
        )

    def _parse(self, arguments: Mapping[str, Any]) -> PolicyDecision | None:
        """参数形状校验;非法返回确定性拒绝,合法返回 None。"""
        store_id = arguments.get("store_id")
        sku = arguments.get("sku")
        price = arguments.get("target_price")
        valid_shape = (
            isinstance(store_id, str)
            and isinstance(sku, str)
            and isinstance(price, Decimal)
        )
        if valid_shape:
            try:
                uuid.UUID(store_id)
            except ValueError:
                valid_shape = False
        if valid_shape:
            return None
        return self._deny(
            POLICY_LISTING_NOT_FOUND,
            "price update target cannot be resolved from validated arguments",
            expected_new_price=None,
        )

    def _deny(
        self,
        reason_code: str,
        safe_message: str,
        *,
        expected_new_price: Decimal | None,
    ) -> PricePolicyDecision:
        return PricePolicyDecision(
            allowed=False,
            reason_code=reason_code,
            safe_message=safe_message,
            policy_version=self._config.policy_version,
            listing_version=None,
            expected_new_price=expected_new_price,
        )


class _AcceptedConfirmationLookup:
    """已接受确认查询:指纹/Actor/过期校验委托 ConfirmationService.verify。"""

    def __init__(self, *, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    async def __call__(
        self, *, tool_call_id: uuid.UUID, stable_key: str, actor_id: uuid.UUID
    ) -> bool:
        del stable_key
        request = (
            await self._session.scalars(
                select(ConfirmationRequest)
                .where(
                    ConfirmationRequest.workspace_id == self._workspace_id,
                    ConfirmationRequest.tool_call_id == tool_call_id,
                )
                .order_by(ConfirmationRequest.created_at.desc(), ConfirmationRequest.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if request is None:
            return False
        if request.status is not ConfirmationRequestStatus.ACCEPTED:
            return False
        if request.actor_id != actor_id:
            return False
        result = await ConfirmationService(
            self._session, workspace_id=self._workspace_id, actor_id=actor_id
        ).verify(request.id, current_resource_versions=dict(request.resource_versions))
        return result.valid


class _ProductReadToolHandler:
    """读 Tool 执行端点:按参数形状确定性分派到三个只读 Tool。"""

    def __init__(
        self, *, engine: AsyncEngine, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        self._engine = engine
        self._workspace_id = workspace_id
        self._actor_id = actor_id

    async def read(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        deadline: datetime,
    ) -> Any:
        del deadline  # 读 Tool 超时由 Loop 预算覆盖。
        arguments = dict(normalized_arguments)
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            actor = await ActorContextBuilder(session).build(
                actor_id=self._actor_id,
                workspace_id=self._workspace_id,
                request_id=f"read-{uuid.uuid4()}",
            )
            store_ids = _store_ids_of(arguments)
            result: Any
            if "query" in arguments:
                query = arguments.get("query")
                result = await search_products(
                    actor=actor,
                    session=session,
                    query=query if isinstance(query, str) else None,
                    store_ids=store_ids,
                )
            elif "store_id" in arguments:
                result = await get_product_snapshot(
                    actor=actor,
                    session=session,
                    store_id=str(arguments["store_id"]),
                    sku=str(arguments.get("sku", "")),
                )
            elif "sku" in arguments:
                result = await compare_listing_prices(
                    actor=actor,
                    session=session,
                    sku=str(arguments["sku"]),
                    store_ids=store_ids,
                )
            else:
                result = await search_products(
                    actor=actor, session=session, query=None, store_ids=()
                )
            return ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status="OK",
                payload=result.model_dump(mode="json"),
            )
        finally:
            await session.close()


def _store_ids_of(arguments: Mapping[str, Any]) -> Sequence[str]:
    raw = arguments.get("store_ids")
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return tuple(str(value) for value in raw)
    return ()


class _CommerceGateway:
    """把商品域写网关实现到 MockCommerceAdapter(apps → adapters 桥接)。"""

    def __init__(self, adapter: MockCommerceAdapter) -> None:
        self._adapter = adapter

    async def execute(self, command: UpdatePriceWriteCommand) -> UpdatePriceWriteReceipt:
        receipt: AdapterExecutionReceipt = await self._adapter.execute(
            AdapterPriceCommand(
                operation_id=command.operation_id,
                workspace_id=command.workspace_id,
                store_id=command.store_id,
                listing_id=command.listing_id,
                target_price=command.target_price,
                currency=command.currency,
                expected_version=command.expected_version,
            )
        )
        return UpdatePriceWriteReceipt(
            status=receipt.status.value,
            before_price=receipt.before_price,
            after_price=receipt.after_price,
            listing_version=receipt.listing_version,
            external_ref=receipt.external_ref,
            reason_code=receipt.reason_code,
        )


# ---------------------------------------------------------------------------
# 门面:驱动方(测试/CLI)看到的运行面
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunView:
    """Run 的只读视图(挂起请求 ID 供 answer/confirm 使用)。"""

    run_id: uuid.UUID
    status: RunStatus
    pending_confirmation_id: uuid.UUID | None
    pending_input_id: uuid.UUID | None


@dataclass(frozen=True)
class ToolCallSnapshot:
    """Run 内最新 ToolCall 的断言视图。"""

    tool_call_id: uuid.UUID
    status: str
    failure_code: str | None
    operation_id: uuid.UUID | None
    raw_arguments: Mapping[str, Any]


class ProductRuntime:
    """商品改价运行面门面:管理 RuntimeHost 推进与挂起请求的恢复。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        assembly: RuntimeAssembly,
        tool_registry: ToolRegistry,
        price_config: PricePolicyConfig,
        backend: AgentBackend,
        adapter: MockCommerceAdapter,
        adapter_session: AsyncSession,
        write_handler: UpdatePriceToolHandler,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        membership_id: uuid.UUID,
        capability: Mapping[str, Any],
        worker_id: str,
    ) -> None:
        self._engine = engine
        self._assembly = assembly
        self._tool_registry = tool_registry
        self._price_config = price_config
        self._backend = backend
        self._adapter = adapter
        self._adapter_session = adapter_session
        self._write_handler = write_handler
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._membership_id = membership_id
        self._capability = dict(capability)
        self._answered: dict[str, str] = {}
        self._scope = _ScopeHolder()
        estimator = HeuristicTokenEstimator()
        assembler = ContextAssembler(
            safety_rules=(
                "runtime safety rules: 权限、资源范围、价格策略与用户确认由服务端强制执行"
            ),
            agent_instructions=PRODUCT_AGENT_PROMPT,
            workspace_summary="商品运营工作区:店铺、商品与改价维护。",
            summarizer=ConversationSummarizer(
                backend=_NoopSummaryBackend(),
                estimator=estimator,
                input_budget_tokens=400,
            ),
            estimator=estimator,
        )
        self._host = RuntimeHost(
            session_factory=self._session_factory,
            backend=backend,
            assembler=assembler,
            limits=_LIMITS,
            tool_pins=_RegistryToolPins(tool_registry),
            pipeline_factory=_ProductPipelineFactory(
                engine=engine,
                tool_registry=tool_registry,
                price_config=price_config,
                scope=self._scope,
            ),
            read_handler=_ProductReadToolHandler(
                engine=engine, workspace_id=workspace_id, actor_id=actor_id
            ),
            write_handler=write_handler,
            output_schema=_OUTPUT_SCHEMA,
            adapter_key=ADAPTER_KEY,
            supports_reconciliation=True,
            worker_id=worker_id,
        )
        # 测试接缝:脚本模型与种子标识由夹具注入/读取。
        self.scripted_backend: Any = backend
        self.seed_ids: Any = None

    # -- 资源与属性 -------------------------------------------------------

    def _session_factory(self) -> AsyncSession:
        return AsyncSession(self._engine, expire_on_commit=False)

    async def aclose(self) -> None:
        """关闭平台 Adapter 的长生命周期 Session(组合根生命周期终点)。"""
        await self._adapter_session.close()

    @property
    def assembly(self) -> RuntimeAssembly:
        return self._assembly

    @property
    def price_config(self) -> PricePolicyConfig:
        return self._price_config

    @property
    def agent_prompt(self) -> str:
        return PRODUCT_AGENT_PROMPT

    @property
    def skill_instructions(self) -> str:
        return PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS

    @property
    def adapter(self) -> MockCommerceAdapter:
        return self._adapter

    @property
    def write_handler(self) -> UpdatePriceToolHandler:
        return self._write_handler

    @property
    def capability_ids(self) -> Mapping[str, Any]:
        """组装写入数据库的能力标识(测试断言与清理用)。"""
        return self._capability

    # -- 驱动入口 ---------------------------------------------------------

    async def start(self, message: str) -> RunView:
        """创建 Run、落用户消息与 ADVANCE_RUN Job,并推进到新持久边界。"""
        run_id: uuid.UUID | None = None
        session = self._session_factory()
        try:
            async with session.begin():
                pinned = await RunFactory(
                    session=session,
                    assembly=self._assembly,
                    workspace_id=self._workspace_id,
                    actor_id=self._actor_id,
                    membership_id=self._membership_id,
                    model_config_version_id=self._capability["model_config_version_id"],
                ).create(agent_key=AGENT_KEY)
                run_id = pinned.id
                store = MessageStore(session=session, workspace_id=self._workspace_id)
                sequence = await _next_message_sequence(session, pinned.run.session_id)
                await store.append_user_message(
                    session_id=pinned.run.session_id,
                    run_id=pinned.id,
                    sequence_no=sequence,
                    content=message,
                )
                await JobRepository(session).create(
                    workspace_id=self._workspace_id,
                    run_id=pinned.id,
                    kind=RunJobKind.ADVANCE_RUN,
                )
        finally:
            await session.close()
        assert run_id is not None
        await self._push(run_id)
        return await self.run_view(run_id)

    async def answer(self, request_id: uuid.UUID, values: Mapping[str, str]) -> RunView:
        """回答 Input Request 并恢复推进(合并挂起参数后重走完整管线)。"""
        run_id: uuid.UUID | None = None
        session = self._session_factory()
        try:
            async with session.begin():
                answered = await InputService(
                    session, workspace_id=self._workspace_id, actor_id=self._actor_id
                ).answer(request_id, dict(values))
                run_id = answered.run_id
                await RunService(
                    session=session, workspace_id=self._workspace_id
                ).transition_run(
                    run_id=run_id,
                    target_status=RunStatus.QUEUED,
                    event_kind=_RUN_RESUMED_EVENT,
                    event_payload={"input_request_id": str(request_id)},
                    expected_status=RunStatus.WAITING_INPUT,
                )
                await JobRepository(session).create(
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    kind=RunJobKind.ADVANCE_RUN,
                )
        finally:
            await session.close()
        assert run_id is not None
        self._answered.update({key: str(value) for key, value in values.items()})
        await self._push(run_id)
        return await self.run_view(run_id)

    async def confirm(self, confirmation_id: uuid.UUID) -> RunView:
        """接受确认并恢复推进(挂起 ToolCall 重走完整 PreToolUse 后执行)。"""
        run_id: uuid.UUID | None = None
        session = self._session_factory()
        try:
            async with session.begin():
                request = await ConfirmationService(
                    session, workspace_id=self._workspace_id, actor_id=self._actor_id
                ).accept(confirmation_id)
                run_id = request.run_id
                await RunService(
                    session=session, workspace_id=self._workspace_id
                ).transition_run(
                    run_id=run_id,
                    target_status=RunStatus.QUEUED,
                    event_kind=_RUN_RESUMED_EVENT,
                    event_payload={"confirmation_id": str(confirmation_id)},
                    expected_status=RunStatus.WAITING_CONFIRMATION,
                )
                await JobRepository(session).create(
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    kind=RunJobKind.ADVANCE_RUN,
                )
        finally:
            await session.close()
        assert run_id is not None
        await self._push(run_id)
        return await self.run_view(run_id)

    async def recover(self, run_id: uuid.UUID) -> RunView:
        """响应丢失恢复:回查 WAITING_RECONCILIATION Run,APPLIED 后恢复推进。"""
        for _ in range(_MAX_RECONCILE_ROUNDS):
            view = await self.run_view(run_id)
            if view.status is not RunStatus.WAITING_RECONCILIATION:
                break
            resumed = await self._reconcile_once(run_id)
            if not resumed:
                break
        view = await self.run_view(run_id)
        if view.status is RunStatus.QUEUED:
            await self._ensure_advance_job(run_id)
            await self._push(run_id)
        return await self.run_view(run_id)

    async def advance_leased(
        self, lease: Lease, *, answered: Mapping[str, str] | None = None
    ) -> LeaseOutcome:
        """推进一个已被外层 Worker 领取的租约(Worker 组合根入口)。

        与 :meth:`_push` 的区别:不再次领取 Job——外层 WorkerHost 已完成
        claim(含 QUEUED→RUNNING 迁移),本方法直接以该租约驱动
        ``RuntimeHost.handle_lease``(单事务 AgentLoop 推进),并把"已回答
        字段"视图注入本次推进的 PreToolUse 管线。LeaseOutcome 交由外层
        Worker complete;异常向上冒泡按暂时性错误释放重试。
        """
        self._scope.begin(
            _ScopeContext(
                workspace_id=lease.workspace_id,
                run_id=lease.run_id,
                actor_id=self._actor_id,
                answered=dict(answered or {}),
            )
        )
        try:
            return await self._host.handle_lease(lease)
        finally:
            self._scope.end()

    # -- 断言辅助 ---------------------------------------------------------

    async def run_view(self, run_id: uuid.UUID) -> RunView:
        session = self._session_factory()
        try:
            run = await RunRepository(session).get(self._workspace_id, run_id)
            if run is None:
                raise LookupError(
                    f"run {run_id} not found in workspace {self._workspace_id}"
                )
            confirmation_id = (
                await session.scalars(
                    select(ConfirmationRequest.id)
                    .where(
                        ConfirmationRequest.workspace_id == self._workspace_id,
                        ConfirmationRequest.run_id == run_id,
                        ConfirmationRequest.status == ConfirmationRequestStatus.PENDING,
                    )
                    .order_by(ConfirmationRequest.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
            input_id = (
                await session.scalars(
                    select(InputRequest.id)
                    .where(
                        InputRequest.workspace_id == self._workspace_id,
                        InputRequest.run_id == run_id,
                        InputRequest.status == InputRequestStatus.PENDING,
                    )
                    .order_by(InputRequest.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
            return RunView(
                run_id=run_id,
                status=RunStatus(run.status.value),
                pending_confirmation_id=confirmation_id,
                pending_input_id=input_id,
            )
        finally:
            await session.close()

    async def tool_call_snapshot(self, run_id: uuid.UUID) -> ToolCallSnapshot | None:
        """Run 内最新(ordinal 最大)ToolCall 的快照。"""
        session = self._session_factory()
        try:
            row = (
                await session.scalars(
                    select(ToolCall)
                    .where(
                        ToolCall.workspace_id == self._workspace_id,
                        ToolCall.run_id == run_id,
                    )
                    .order_by(ToolCall.ordinal.desc())
                    .limit(1)
                )
            ).one_or_none()
            if row is None:
                return None
            return ToolCallSnapshot(
                tool_call_id=row.id,
                status=row.status.value,
                failure_code=row.failure_code,
                operation_id=row.operation_id,
                raw_arguments=dict(row.raw_arguments),
            )
        finally:
            await session.close()

    async def authoritative_price(self, store_id: str, sku: str) -> Decimal:
        """权威价格:经 MockCommerceAdapter 真实写入的 listings.current_price。"""
        session = self._session_factory()
        try:
            listing = await _resolve_listing(
                session, self._workspace_id, uuid.UUID(store_id), sku
            )
            if listing is None:
                raise LookupError(f"listing for store {store_id} sku {sku} not found")
            return listing.current_price
        finally:
            await session.close()

    # -- 内部:推进与回查 -------------------------------------------------

    async def _push(self, run_id: uuid.UUID) -> None:
        """设置推进上下文并驱动一轮 Worker(claim → advance → complete)。"""
        self._scope.begin(
            _ScopeContext(
                workspace_id=self._workspace_id,
                run_id=run_id,
                actor_id=self._actor_id,
                answered=dict(self._answered),
            )
        )
        try:
            processed = await self._host.run_worker_once()
        finally:
            self._scope.end()
        if not processed:
            raise RuntimeError(f"no job was claimed while advancing run {run_id}")

    async def _reconcile_once(self, run_id: uuid.UUID) -> bool:
        """领取 RECONCILE_OPERATION Job,以平台回查结论提交;返回是否可恢复推进。"""
        session = self._session_factory()
        try:
            async with session.begin():
                job = await JobRepository(session).create(
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    kind=RunJobKind.RECONCILE_OPERATION,
                )
                lease = await LeaseService(session).claim("product-reconciler")
                if lease is None:
                    return False
                run = await RunRepository(session).get(self._workspace_id, run_id)
                if run is None:
                    raise LookupError(f"run {run_id} vanished")
                tool_call = (
                    await session.scalars(
                        select(ToolCall)
                        .where(
                            ToolCall.workspace_id == self._workspace_id,
                            ToolCall.run_id == run_id,
                        )
                        .order_by(ToolCall.ordinal.desc())
                        .limit(1)
                    )
                ).one_or_none()
                if tool_call is None or tool_call.operation_id is None:
                    return False
                expected_price = _price_of(tool_call)
                self._adapter_session.expire_all()
                verdict = await self._adapter.reconcile(
                    str(tool_call.operation_id), expected_price=expected_price
                )
                outcome = await ReconciliationService(
                    session,
                    workspace_id=self._workspace_id,
                    actor_id=self._actor_id,
                    run_id=run_id,
                    operation_id=tool_call.operation_id,
                    lease_token=lease.lease_token,
                    expected_state_version=run.state_version,
                    reconcile_budget=_LIMITS.reconcile_budget,
                ).apply_result(verdict)
                await LeaseService(session).complete(job.id, lease.lease_token)
                return outcome.next_job_kind is RunJobKind.ADVANCE_RUN
        finally:
            await session.close()

    async def _ensure_advance_job(self, run_id: uuid.UUID) -> None:
        session = self._session_factory()
        try:
            async with session.begin():
                await JobRepository(session).create(
                    workspace_id=self._workspace_id,
                    run_id=run_id,
                    kind=RunJobKind.ADVANCE_RUN,
                )
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# 组合根入口
# ---------------------------------------------------------------------------


async def build_product_runtime(
    engine: AsyncEngine,
    *,
    backend: AgentBackend,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    membership_id: uuid.UUID,
    price_config: PricePolicyConfig,
    worker_id: str = "product-worker",
) -> ProductRuntime:
    """组装 ProductMaintenance Agent 的完整运行面(启动校验失败即拒绝)。"""
    capability = await _bootstrap_capabilities(engine, workspace_id)

    agent_registry = AgentRegistry()
    agent_registry.register(
        AgentVersionRecord(
            stable_key=AGENT_KEY,
            version_id=capability["agent_version_id"],
            version=1,
            agent_id=capability["agent_id"],
            definition=PRODUCT_AGENT_DEFINITION,
        )
    )
    skill_registry = SkillRegistry()
    skill_registry.register(
        SkillVersionRecord(
            stable_key=SKILL_KEY,
            version_id=capability["skill_version_id"],
            version=1,
            workspace_id=None,
            content=PRODUCT_MAINTENANCE_SKILL,
        )
    )

    adapter_session = AsyncSession(engine, expire_on_commit=False)
    adapter = MockCommerceAdapter(adapter_session)
    write_handler = UpdatePriceToolHandler(
        workspace_id=workspace_id,
        session=adapter_session,
        gateway=_CommerceGateway(adapter),
        policy_version=price_config.policy_version,
    )

    tool_registry = ToolRegistry()
    for key, definition in _tool_definitions():
        handler: Any
        if key == STABLE_KEY_UPDATE_PRICE:
            handler = write_handler
        else:
            handler = _read_handler_of(key)
        tool_registry.register(
            ToolVersionRecord(
                stable_key=key,
                version_id=capability["tool_version_ids"][key],
                version=definition.version,
                workspace_id=None,
                source_kind="LOCAL",
                effect=definition.effect,
                input_schema=definition.input_json_schema,
                output_schema=definition.output_json_schema,
                schema_hash=compute_schema_hash(
                    definition.input_json_schema, definition.output_json_schema
                ),
                required_permissions=definition.required_permissions,
            )
        )
        tool_registry.bind(
            ToolBinding(
                definition=definition,
                handler=handler,
                write_adapter_capabilities=(
                    _WRITE_ADAPTER_CAPABILITIES
                    if definition.effect is ToolEffectKind.WRITE
                    else frozenset()
                ),
            )
        )

    backends = BackendRegistry()
    backends.register(BACKEND_KEY, backend)
    assembly = RuntimeAssembly(
        agents=agent_registry,
        skills=skill_registry,
        tools=tool_registry,
        sources=SourceRegistry(),
        backends=backends,
        backend_key=BACKEND_KEY,
    )
    assembly.validate()

    return ProductRuntime(
        engine=engine,
        assembly=assembly,
        tool_registry=tool_registry,
        price_config=price_config,
        backend=backend,
        adapter=adapter,
        adapter_session=adapter_session,
        write_handler=write_handler,
        workspace_id=workspace_id,
        actor_id=actor_id,
        membership_id=membership_id,
        capability=capability,
        worker_id=worker_id,
    )


def _read_handler_of(stable_key: str) -> Any:
    """只读 Tool 的可执行实现(按稳定 Key 分派)。"""
    if stable_key == SEARCH_PRODUCTS_DEFINITION.stable_key:
        return search_products
    if stable_key == GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key:
        return get_product_snapshot
    if stable_key == COMPARE_LISTING_PRICES_DEFINITION.stable_key:
        return compare_listing_prices
    raise LookupError(f"no read handler for tool {stable_key}")


# ---------------------------------------------------------------------------
# 共享查询辅助
# ---------------------------------------------------------------------------


async def _resolve_listing(
    session: AsyncSession, workspace_id: uuid.UUID, store_id: uuid.UUID, sku: str
) -> Listing | None:
    """按 (workspace, store, sku) 读取 Listing(供 Policy 与权威回读)。"""
    stmt = (
        select(Listing)
        .join(Product, Listing.product_id == Product.id)
        .where(
            Listing.workspace_id == workspace_id,
            Listing.store_id == store_id,
            Product.sku == sku,
        )
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one_or_none()


async def _current_user_message(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> str:
    """当前用户消息:该 Run 会话内最后一条 USER 消息(Provenance 证据来源)。"""
    run = await RunRepository(session).get(workspace_id, run_id)
    if run is None:
        return ""
    row = (
        await session.scalars(
            select(Message)
            .where(
                Message.workspace_id == workspace_id,
                Message.session_id == run.session_id,
                Message.role == MessageRole.USER,
            )
            .order_by(Message.sequence_no.desc())
            .limit(1)
        )
    ).one_or_none()
    return row.content if row is not None else ""


async def _next_message_sequence(session: AsyncSession, session_id: uuid.UUID) -> int:
    """会话内消息序号:max+1(uq_messages_session_sequence)。"""
    current = await session.scalar(
        select(func.max(Message.sequence_no)).where(Message.session_id == session_id)
    )
    return 1 if current is None else int(current) + 1


def cast_decimal(value: Any) -> Decimal:
    """前置条件:调用方已确认 ``isinstance(value, Decimal)``。"""
    result: Decimal = value
    return result


def _price_of(tool_call: ToolCall) -> Decimal:
    """回查期望价:ToolCall 挂起参数(规范化优先)中的 target_price。"""
    arguments = (
        tool_call.normalized_arguments
        if tool_call.normalized_arguments is not None
        else tool_call.raw_arguments
    )
    raw = dict(arguments).get("target_price")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise RuntimeError(
            f"tool call {tool_call.id} has no resolvable target_price for reconcile"
        ) from error


bootstrap_product_capabilities = _bootstrap_capabilities
"""启动期能力版本落库的公开名称(供 Eval 组合根等外部驱动方导入)。"""


__all__ = [
    "ProductRuntime",
    "RunView",
    "ToolCallSnapshot",
    "bootstrap_product_capabilities",
    "build_product_runtime",
]
