"""商品域组合核心与门面(计划 03 Task 6;计划 05 Task 2 下沉)。

``build_product_application`` 是 ProductMaintenance Agent 的组装核心:

- 只消费已发布能力快照(:class:`~composition.provisioning.product_operations.ProductOperationsCapabilities`),
  校验其 Version ID 均已发布后注册进程内 Registry,执行
  :class:`~runtime.capabilities.assembly.RuntimeAssembly` 启动校验;
  本模块不写 Agent/Skill/Tool/Model 配置表(发布由 Provisioner 负责);
- 组装 PreToolUse 管线工厂(注入商品域各阶段实现:输入归一化、参数
  Provenance、InputRequirement、店铺资源解析、价格 Policy、执行模式与
  确认校验)、:class:`~runtime.orchestration.context.ContextAssembler`、
  读写 Tool Handler 与 :class:`~runtime.orchestration.runtime.Runtime`;
- 平台桥接:把 :class:`~agents.product_operations.tools.update_price.UpdatePriceGateway`
  实现到 :class:`~adapters.commerce.mock_adapter.MockCommerceAdapter`。

模块归属:本组合核心同时被 apps/worker(生产组合根)与 eval(Eval Runner,
计划 05 Task 2)复用,而 eval 不得 import apps(方向:eval → runtime/
agents/adapters)。本模块原本位于 apps/worker/assembly.py;由于 runtime 不
import agents/adapters、agents 不 import adapters(架构文档 §2),而
adapters 已依赖 agents(:mod:`adapters.commerce.mock_adapter` 先例),故
下沉到本模块作为单一事实源;``apps/worker/assembly.py`` 保留为兼容导出。

``ProductApplication`` 门面供驱动方(Worker、测试、CLI、Eval)使用:``start``
包装 RunFactory → ADVANCE_RUN Job → Runtime Worker 推进;``answer`` /
``confirm`` 用 Input/Confirmation 服务处理挂起请求后恢复推进;``recover``
对响应丢失的写操作调度 reconcile 回查后恢复推进。Worker 进程按 Run 亲和
持有已回答字段的内存视图(真实部署中应由输入请求持久化重建)。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.protocol import (
    CommerceAdapter,
    ReconciliationCommand,
)
from adapters.commerce.protocol import ExecutionReceipt as AdapterExecutionReceipt
from adapters.commerce.protocol import (
    UpdatePriceCommand as AdapterPriceCommand,
)
from agents.product_operations.agent import (
    AGENT_KEY,
    AGENT_VERSION,
    LEGACY_AGENT_KEY,
    LEGACY_AGENT_VERSION,
    LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION,
    PRODUCT_AGENT_DEFINITION,
)
from agents.product_operations.catalog.tools import (
    SEARCH_PRODUCTS_DEFINITION,
    search_products,
)
from agents.product_operations.input.profiles import (
    TOOL_ARGUMENT_FIELDS,
    p1_fields_for,
    product_registration,
)
from agents.product_operations.inventory.gateway import InventoryGateway
from agents.product_operations.inventory.policy import InventoryPolicyConfig
from agents.product_operations.inventory.service import AdjustInventoryQuantityHandler
from agents.product_operations.inventory.tools import (
    ADJUST_INVENTORY_QUANTITY_DEFINITION,
    GET_INVENTORY_LEVEL_DEFINITION,
    get_inventory_level,
)
from agents.product_operations.listing.gateway import ListingGateway
from agents.product_operations.listing.policy import ListingPolicyConfig
from agents.product_operations.listing.service import (
    PublishListingHandler,
    UnpublishListingHandler,
)
from agents.product_operations.listing.tools import (
    GET_LISTING_DEFINITION,
    PUBLISH_LISTING_DEFINITION,
    UNPUBLISH_LISTING_DEFINITION,
    get_listing,
)
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.policy.price_pretool import PricePreToolPolicy
from agents.product_operations.policy.resources import (
    StoreIdsResourceResolver,
    StoreResourceResolver,
)
from agents.product_operations.pricing.configuration import (
    PRICING_POLICY_STABLE_KEY,
    PublishedPricingPolicy,
)
from agents.product_operations.pricing.gateway import PricingGateway
from agents.product_operations.pricing.service import SetListingPriceHandler
from agents.product_operations.pricing.tools import (
    COMPARE_LISTING_PRICES_DEFINITION,
    SET_LISTING_PRICE_DEFINITION,
    compare_listing_prices,
)
from agents.product_operations.prompt import PRODUCT_AGENT_PROMPT
from agents.product_operations.skill import (
    LEGACY_PRODUCT_MAINTENANCE_SKILL,
    LEGACY_SKILL_VERSION,
    PRODUCT_MAINTENANCE_SKILL,
    PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS,
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
from agents.product_operations.tools.handlers import (
    CompareListingPricesHandler,
    GetProductSnapshotHandler,
    SearchProductsHandler,
)
from agents.product_operations.tools.update_price import (
    STABLE_KEY_UPDATE_PRICE,
    UpdatePriceToolHandler,
    UpdatePriceWriteCommand,
    UpdatePriceWriteReceipt,
)
from composition.provisioning.product_operations import (
    ProductOperationsCapabilities,
    legacy_product_tool_definitions,
    load_published_pricing_policy,
    product_tool_definitions,
    require_published_capabilities,
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
from runtime.capabilities.skill_activation import (
    LOAD_SKILL_HANDLER,
    LOAD_SKILL_KEY,
)
from runtime.capabilities.validation import compute_schema_hash
from runtime.commands.session import SessionCommandService
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.pretool.decisions import (
    ExecutionModeDecision,
    PreToolDecision,
)
from runtime.execution.pretool.pipeline import (
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.profiles import (
    PreToolProfileContext,
    PreToolProfileRegistration,
    runtime_control_registration,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.reconciliation import ReconciliationService
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.execution.tool_result_processor import ToolExecutionReceipt
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interfaces.state import RunStatus
from runtime.interfaces.tools import ToolEffect as ToolEffectKind
from runtime.jobs.lease_service import Lease, LeaseOutcome, LeaseService
from runtime.orchestration.agent_loop import ToolCallEvaluator, ToolPin
from runtime.orchestration.context import ContextAssembler, SkillCatalogEntry
from runtime.orchestration.limits import RunLimits
from runtime.orchestration.messages import MessageStore
from runtime.orchestration.runtime import Runtime
from runtime.orchestration.summarization import ConversationSummarizer
from runtime.orchestration.token_estimator import HeuristicTokenEstimator
from runtime.persistence.base import (
    ConfirmationRequestStatus,
    ExecutionMode,
    InputRequestStatus,
    OperationStatus,
    RunJobKind,
    ToolEffect,
)
from runtime.persistence.models.configuration import AgentVersion as AgentVersionRow
from runtime.persistence.models.execution import Operation, ToolCall
from runtime.persistence.models.identity import Workspace as WorkspaceRow
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
)
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.run_snapshots import PolicySnapshotPin
from runtime.persistence.repositories.runs import RunRepository


ADAPTER_KEY: Final = "mock-commerce"
BACKEND_KEY: Final = "product-scripted"

_WRITE_ADAPTER_CAPABILITIES: Final[frozenset[str]] = frozenset({"operation", "reconciliation"})

_MAX_RECONCILE_ROUNDS: Final = 5


# ---------------------------------------------------------------------------
# 进程内协作对象
# ---------------------------------------------------------------------------


class _RegistryToolPins:
    """基于 ToolRegistry 的 ToolPin 解析(Run 钉住版本即注册版本)。

    全部已装载版本都建 Pin:新 Run 按当前版本解析,恢复旧 Run 时按 Run 钉住的
    精确 Version ID 取回旧 ToolVersion 及其 Handler。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._by_key: dict[str, ToolPin] = {}
        self._by_id: dict[uuid.UUID, ToolPin] = {}
        for record in registry.all_records():
            if record.effect is None:
                continue
            binding = registry.binding_for_version(record.version_id)
            self._by_id[record.version_id] = ToolPin(
                stable_key=record.stable_key,
                version=record.version,
                version_id=record.version_id,
                effect=ToolEffect(record.effect.value),
                handler=binding.handler if binding is not None else None,
                output_schema=record.output_schema,
                execution_profile=(
                    binding.execution_profile if binding is not None else None
                ),
            )
        for record in registry.records():
            pin = self._by_id.get(record.version_id)
            if pin is not None:
                self._by_key[record.stable_key] = pin

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


ReadToolInvoker = Callable[
    [ActorContext, AsyncSession, Mapping[str, Any]], Awaitable[Any]
]


class _P1ReadHandler:
    """把 P1 只读 Service 适配到 Runtime 的 ``read`` Handler 协议。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        invoke: ReadToolInvoker,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._invoke = invoke

    async def read(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        deadline: datetime,
    ) -> Any:
        del deadline
        session = self._session_factory()
        try:
            actor = await ActorContextBuilder(session).build(
                actor_id=self._actor_id,
                workspace_id=self._workspace_id,
                request_id=f"read-{tool_call_id}",
            )
            result = await self._invoke(actor, session, normalized_arguments)
            return ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status="OK",
                payload=result.model_dump(mode="json"),
            )
        finally:
            await session.close()


WriteHandlerFactory = Callable[[ActorContext], Any]


class _ScopedWriteHandler:
    """延迟构造请求 Actor，避免应用装配依赖尚未建立的成员上下文。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        factory: WriteHandlerFactory,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._factory = factory

    async def _handler(self, request_id: str) -> Any:
        actor = await ActorContextBuilder(self._session).build(
            actor_id=self._actor_id,
            workspace_id=self._workspace_id,
            request_id=request_id,
        )
        await self._session.rollback()
        return self._factory(actor)

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> Any:
        handler = await self._handler(f"write-prepare-{uuid.uuid4()}")
        return await handler.prepare(normalized_arguments=normalized_arguments)

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: Any,
    ) -> Any:
        handler = await self._handler(f"write-execute-{tool_call_id}")
        return await handler.execute_prepared(
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            prepared=prepared,
        )


def _string_sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(str(item) for item in value)
    return ()


async def _search_products_read(
    actor: ActorContext,
    session: AsyncSession,
    arguments: Mapping[str, Any],
) -> Any:
    query = arguments.get("query")
    return await search_products(
        actor=actor,
        session=session,
        query=query if isinstance(query, str) else None,
        store_refs=_string_sequence(arguments.get("store_refs")),
    )


async def _get_listing_read(
    actor: ActorContext,
    session: AsyncSession,
    arguments: Mapping[str, Any],
) -> Any:
    return await get_listing(
        actor=actor,
        session=session,
        store_ref=str(arguments.get("store_ref", "")),
        sku=str(arguments.get("sku", "")),
    )


async def _compare_listing_prices_read(
    actor: ActorContext,
    session: AsyncSession,
    arguments: Mapping[str, Any],
) -> Any:
    return await compare_listing_prices(
        actor=actor,
        session=session,
        sku=str(arguments.get("sku", "")),
        store_refs=_string_sequence(arguments.get("store_refs")),
    )


async def _get_inventory_level_read(
    actor: ActorContext,
    session: AsyncSession,
    arguments: Mapping[str, Any],
) -> Any:
    location_ref = arguments.get("location_ref")
    return await get_inventory_level(
        actor=actor,
        session=session,
        store_ref=str(arguments.get("store_ref", "")),
        sku=str(arguments.get("sku", "")),
        location_ref=location_ref if isinstance(location_ref, str) else None,
    )


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

    PreToolUse 业务扩展不再由本工厂按 ``stable_key`` 全局注入:每个 Tool 的
    ``ToolBinding.pretool_registration`` 自带工厂,在 tool_resolution 之后用
    本次请求级上下文(Session / Actor / ToolCall / Run)延迟创建 Profile。
    本工厂只负责构造 Runtime 统一依赖与本次推进的上下文。
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        tool_registry: ToolRegistry,
        scope: _ScopeHolder,
    ) -> None:
        self._engine = engine
        self._tool_registry = tool_registry
        self._scope = scope

    def __call__(self) -> ToolCallEvaluator:
        return _ScopedProductPipeline(
            engine=self._engine,
            context=self._scope.require(),
            tool_registry=self._tool_registry,
        )


class _ScopedProductPipeline:
    """单次推进的 PreToolUse 管线;评估结束后关闭自有读取 Session。

    只构造 Runtime 统一依赖与本次 actor/context;商品业务 Profile 由各
    ToolBinding 在注册时携带的工厂延迟创建,应用启动时不持有请求级 Session。
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        context: _ScopeContext,
        tool_registry: ToolRegistry,
    ) -> None:
        self._engine = engine
        self._context = context
        self._tool_registry = tool_registry

    async def evaluate(self, tool_call: ToolCallRequest) -> PreToolDecision:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            actor = await ActorContextBuilder(session).build(
                actor_id=self._context.actor_id,
                workspace_id=self._context.workspace_id,
                request_id=f"pretool-{tool_call.tool_call_id}",
            )

            async def resolve_execution_mode(*, effect: ToolEffectKind) -> ExecutionModeDecision:
                workspace = await session.get(WorkspaceRow, self._context.workspace_id)
                if workspace is None:
                    return ExecutionModeDecision(mode="DENY")
                if effect is not ToolEffectKind.WRITE:
                    return ExecutionModeDecision(mode="READ_ONLY")
                if workspace.execution_mode is ExecutionMode.AUTO_EXECUTE:
                    return ExecutionModeDecision(mode="AUTO")
                if workspace.execution_mode is ExecutionMode.CONFIRM_WRITES:
                    return ExecutionModeDecision(mode="CONFIRM")
                return ExecutionModeDecision(mode="DENY")

            dependencies = PreToolDependencies(
                tool_registry=RuntimeToolRegistry(self._tool_registry),
                schema_validator=RuntimeSchemaValidator(),
                authorizer=Authorizer(),
                execution_mode_resolver=resolve_execution_mode,
                accepted_confirmation_lookup=_AcceptedConfirmationLookup(
                    session=session, workspace_id=self._context.workspace_id
                ),
                profile_context=PreToolProfileContext(
                    session=session,
                    actor=actor,
                    request=tool_call,
                    run_id=self._context.run_id,
                    answered=dict(self._context.answered),
                ),
            )
            pipeline = PreToolUsePipeline(
                actor=actor,
                stages=default_stages(dependencies),
            )
            return await pipeline.evaluate(tool_call)
        finally:
            await session.close()


class _AcceptedConfirmationLookup:
    """已接受确认查询:指纹/Actor/过期校验委托 ConfirmationService.verify。"""

    def __init__(self, *, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    async def __call__(
        self,
        *,
        tool_call_id: uuid.UUID,
        stable_key: str,
        actor_id: uuid.UUID,
        policy_version: int | None,
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
        # 资源版本用当前值(而非请求自带的副本),并把当前策略修订一并喂入指纹重算:
        # 确认之后资源被改动或价格策略被重新发布,旧确认都必须失效。
        result = await ConfirmationService(
            self._session, workspace_id=self._workspace_id, actor_id=actor_id
        ).verify(
            request.id,
            current_resource_versions=dict(request.resource_versions),
            policy_version=policy_version,
        )
        return result.valid


class _CommerceGateway:
    """把商品写命令映射到注入的商品平台 Adapter。"""

    def __init__(self, adapter: CommerceAdapter) -> None:
        self._adapter = adapter

    async def execute(self, command: UpdatePriceWriteCommand) -> UpdatePriceWriteReceipt:
        receipt: AdapterExecutionReceipt = await self._adapter.execute(
            AdapterPriceCommand(
                operation_id=command.operation_id,
                workspace_id=command.workspace_id,
                store_id=command.store_id,
                listing_id=command.listing_id,
                external_store_ref=command.external_store_ref,
                external_product_ref=command.external_product_ref,
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
            retryable=receipt.retryable,
        )


# ---------------------------------------------------------------------------
# 门面:驱动方(测试/CLI)看到的商品应用
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
    normalized_arguments: Mapping[str, Any] | None


class ProductApplication:
    """商品改价应用门面:管理 Runtime 推进与挂起请求的恢复。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        assembly: RuntimeAssembly,
        tool_registry: ToolRegistry,
        pricing_policy: PublishedPricingPolicy,
        backend: AgentBackend,
        adapter: CommerceAdapter,
        adapter_session: AsyncSession,
        write_handler: UpdatePriceToolHandler,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        membership_id: uuid.UUID,
        capabilities: ProductOperationsCapabilities,
        worker_id: str,
    ) -> None:
        self._engine = engine
        self._assembly = assembly
        self._tool_registry = tool_registry
        self._pricing_policy = pricing_policy
        self._backend = backend
        self._adapter = adapter
        self._adapter_session = adapter_session
        self._write_handler = write_handler
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._membership_id = membership_id
        self._capabilities = capabilities
        self._answered: dict[str, str] = {}
        self._scope = _ScopeHolder()
        # 运行限制是 AgentVersion 定义的一部分:同一份定义同时驱动 Runtime
        # 构造与 Reconciliation 预算,不再持有独立的模块级常量。
        self._run_limits = RunLimits.from_definition(
            PRODUCT_AGENT_DEFINITION.loop_limits
        )
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
            skill_catalog=tuple(
                SkillCatalogEntry(
                    stable_key=record.stable_key,
                    name=record.content.name or record.stable_key,
                    description=record.content.description,
                    version=record.version,
                )
                for record in assembly.skills.records()
            ),
            tool_definitions=tuple(
                definition for _, definition in product_tool_definitions()
            ),
        )
        self._runtime = Runtime(
            session_factory=self._session_factory,
            backend=backend,
            assembler=assembler,
            limits=self._run_limits,
            tool_pins=_RegistryToolPins(tool_registry),
            pipeline_factory=_ProductPipelineFactory(
                engine=engine,
                tool_registry=tool_registry,
                scope=self._scope,
            ),
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
    def run_limits(self) -> RunLimits:
        """当前应用实际使用的运行限制(由 AgentVersion 定义组装)。"""
        return self._run_limits

    @property
    def pricing_policy(self) -> PublishedPricingPolicy:
        """本次装配实际读取的已发布价格策略(精确行 + 已校验配置)。"""
        return self._pricing_policy

    @property
    def agent_prompt(self) -> str:
        return PRODUCT_AGENT_PROMPT

    @property
    def skill_instructions(self) -> str:
        return PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS

    @property
    def adapter(self) -> CommerceAdapter:
        return self._adapter

    @property
    def write_handler(self) -> UpdatePriceToolHandler:
        return self._write_handler

    @property
    def capability_ids(self) -> Mapping[str, Any]:
        """组装写入数据库的能力标识(测试断言与清理用)。"""
        return self._capabilities.as_ids()

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
                    model_config_version_id=self._capabilities.model_config_version_id,
                    policy_pins=(
                        PolicySnapshotPin(
                            policy_key=PRICING_POLICY_STABLE_KEY,
                            version_id=self._pricing_policy.version_id,
                            config_hash=self._pricing_policy.config_hash,
                        ),
                    ),
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
        session = self._session_factory()
        try:
            async with session.begin():
                decision = await SessionCommandService(
                    session, actor_id=self._actor_id
                ).answer_input_request(request_id, dict(values))
                run_id = decision.run_id
        finally:
            await session.close()
        self._answered.update({key: str(value) for key, value in values.items()})
        await self._push(run_id)
        return await self.run_view(run_id)

    async def confirm(self, confirmation_id: uuid.UUID) -> RunView:
        """接受确认并恢复推进(挂起 ToolCall 重走完整 PreToolUse 后执行)。"""
        session = self._session_factory()
        try:
            async with session.begin():
                decision = await SessionCommandService(
                    session, actor_id=self._actor_id
                ).accept_confirmation(confirmation_id)
                run_id = decision.run_id
        finally:
            await session.close()
        await self._push(run_id)
        return await self.run_view(run_id)

    async def append_message(self, session_id: uuid.UUID, message: str) -> RunView:
        """在既有 Session 中提交下一条用户消息并推进新 Run。"""
        session = self._session_factory()
        try:
            async with session.begin():
                dispatched = await SessionCommandService(
                    session, actor_id=self._actor_id
                ).append_message(session_id, content=message)
                run_id = dispatched.run.id
        finally:
            await session.close()
        await self._push(run_id)
        return await self.run_view(run_id)

    async def reject_confirmation(self, confirmation_id: uuid.UUID) -> RunView:
        """拒绝写前确认并恢复原 Run;挂起写调用经 PreToolUse 拒绝后结束。"""
        session = self._session_factory()
        try:
            async with session.begin():
                decision = await SessionCommandService(
                    session, actor_id=self._actor_id
                ).reject_confirmation(confirmation_id)
                run_id = decision.run_id
        finally:
            await session.close()
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
        ``Runtime.handle_lease``(单事务 AgentLoop 推进),并把"已回答
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
            return await self._runtime.handle_lease(lease)
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
                normalized_arguments=(
                    dict(row.normalized_arguments)
                    if row.normalized_arguments is not None
                    else None
                ),
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
            processed = await self._runtime.run_worker_once()
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
                if lease.run_id != run_id or lease.workspace_id != self._workspace_id:
                    return False
                run = await RunRepository(session).get(self._workspace_id, run_id)
                if run is None:
                    raise LookupError(f"run {run_id} vanished")
                operations = (
                    await session.scalars(
                        select(Operation)
                        .join(ToolCall, ToolCall.id == Operation.tool_call_id)
                        .where(
                            Operation.workspace_id == self._workspace_id,
                            ToolCall.workspace_id == self._workspace_id,
                            ToolCall.run_id == run_id,
                            Operation.status == OperationStatus.UNKNOWN,
                        )
                    )
                ).all()
                if len(operations) != 1:
                    return False
                operation = operations[0]
                tool_call = await session.get(ToolCall, operation.tool_call_id)
                if (
                    tool_call is None
                    or tool_call.workspace_id != self._workspace_id
                    or tool_call.operation_id != operation.operation_id
                ):
                    raise LookupError(
                        f"tool call for UNKNOWN operation {operation.operation_id} is missing"
                    )
                arguments = (
                    tool_call.normalized_arguments
                    if tool_call.normalized_arguments is not None
                    else tool_call.raw_arguments
                )
                store_id = uuid.UUID(str(arguments["store_id"]))
                sku = str(arguments["sku"])
                listing = await _resolve_listing(
                    session, self._workspace_id, store_id, sku
                )
                store = await session.get(Store, store_id)
                if listing is None or store is None:
                    raise LookupError(
                        f"platform resource for UNKNOWN operation {operation.operation_id} is missing"
                    )
                reconcile_command = ReconciliationCommand(
                    operation_id=str(operation.operation_id),
                    workspace_id=self._workspace_id,
                    store_id=store_id,
                    listing_id=listing.id,
                    external_store_ref=store.external_store_ref,
                    external_product_ref=listing.external_product_ref,
                    expected_price=_price_of(tool_call),
                    currency=listing.currency,
                )
                operation_id = operation.operation_id
                lease_token = lease.lease_token
                expected_state_version = run.state_version
        finally:
            await session.close()

        # 平台回查可能耗时;此处不得占用 Runtime 状态事务。
        self._adapter_session.expire_all()
        verdict = await self._adapter.reconcile(reconcile_command)

        session = self._session_factory()
        try:
            async with session.begin():
                outcome = await ReconciliationService(
                    session,
                    workspace_id=self._workspace_id,
                    actor_id=self._actor_id,
                    run_id=run_id,
                    operation_id=operation_id,
                    lease_token=lease_token,
                    expected_state_version=expected_state_version,
                    reconcile_budget=self._run_limits.reconcile_budget,
                    write_retry_budget=self._run_limits.write_retry_budget,
                ).apply_result(verdict)
                await LeaseService(session).complete(job.id, lease_token)
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


async def build_product_application(
    engine: AsyncEngine,
    *,
    capabilities: ProductOperationsCapabilities,
    backend: AgentBackend,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    membership_id: uuid.UUID,
    worker_id: str = "product-worker",
    commerce_adapter: CommerceAdapter | None = None,
    adapter_key: str = ADAPTER_KEY,
) -> ProductApplication:
    """组装 ProductMaintenance Agent;默认 Mock,可注入专用 CommerceAdapter。

    ``capabilities`` 必须来自已发布快照(:class:`ProductOperationsProvisioner`);
    本函数只校验并消费,不写任何配置表。

    价格规则**不在本函数的参数里**:它从 ``capabilities.pricing_policy_version_id``
    指向的已发布行读取一次,同一个 :class:`PublishedPricingPolicy` 注入 PreTool
    业务策略与执行 Handler。任何"从常量、环境变量或 Prompt 回退配置"的路径都不存在,
    因此装配期就能保证"预检、执行与确认指纹用的是同一版规则"。
    """
    await require_published_capabilities(engine, capabilities)
    pricing_policy = await load_published_pricing_policy(
        engine,
        workspace_id=workspace_id,
        version_id=capabilities.pricing_policy_version_id,
        expected_config_hash=capabilities.pricing_policy_config_hash,
    )

    agent_registry = AgentRegistry()
    agent_registry.register(
        AgentVersionRecord(
            stable_key=AGENT_KEY,
            version_id=capabilities.agent_version_id,
            version=AGENT_VERSION,
            agent_id=capabilities.agent_id,
            definition=PRODUCT_AGENT_DEFINITION,
        )
    )
    compatibility_agent_ids: dict[uuid.UUID, uuid.UUID] = {}
    if capabilities.compatibility_agent_version_ids:
        compatibility_session = AsyncSession(engine, expire_on_commit=False)
        try:
            for version_id in capabilities.compatibility_agent_version_ids:
                row = await compatibility_session.get(AgentVersionRow, version_id)
                if row is not None:
                    compatibility_agent_ids[version_id] = row.agent_id
        finally:
            await compatibility_session.close()
    for version_id in capabilities.compatibility_agent_version_ids:
        agent_id = compatibility_agent_ids.get(version_id)
        if agent_id is None:
            continue
        agent_registry.register(
            AgentVersionRecord(
                stable_key=LEGACY_AGENT_KEY,
                version_id=version_id,
                version=LEGACY_AGENT_VERSION,
                agent_id=agent_id,
                definition=LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION,
            ),
            make_current=False,
        )
    skill_registry = SkillRegistry()
    skill_registry.register(
        SkillVersionRecord(
            stable_key=SKILL_KEY,
            version_id=capabilities.skill_version_id,
            version=SKILL_VERSION,
            workspace_id=None,
            content=PRODUCT_MAINTENANCE_SKILL,
        )
    )
    for version_id in capabilities.compatibility_skill_version_ids:
        skill_registry.register(
            SkillVersionRecord(
                stable_key=SKILL_KEY,
                version_id=version_id,
                version=LEGACY_SKILL_VERSION,
                workspace_id=None,
                content=LEGACY_PRODUCT_MAINTENANCE_SKILL,
            ),
            make_current=False,
        )

    adapter_session = AsyncSession(engine, expire_on_commit=False)
    adapter: CommerceAdapter = (
        commerce_adapter if commerce_adapter is not None else MockCommerceAdapter(adapter_session)
    )
    write_handler = UpdatePriceToolHandler(
        workspace_id=workspace_id,
        session=adapter_session,
        gateway=_CommerceGateway(adapter),
        policy_version=pricing_policy.config.policy_version,
        policy_version_id=pricing_policy.version_id,
        policy_revision=pricing_policy.version,
        policy_config_hash=pricing_policy.config_hash,
    )
    pricing_gateway = cast(PricingGateway, adapter)
    inventory_gateway = cast(InventoryGateway, adapter)
    listing_gateway = cast(ListingGateway, adapter)
    set_listing_price_handler = _ScopedWriteHandler(
        session=adapter_session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        factory=lambda actor: SetListingPriceHandler(
            actor=actor,
            session=adapter_session,
            gateway=pricing_gateway,
            pricing_policy=pricing_policy,
        ),
    )
    adjust_inventory_handler = _ScopedWriteHandler(
        session=adapter_session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        factory=lambda actor: AdjustInventoryQuantityHandler(
            actor=actor,
            session=adapter_session,
            gateway=inventory_gateway,
            policy_config=InventoryPolicyConfig(policy_version="inventory-policy-v1"),
        ),
    )
    supported_capabilities = getattr(
        adapter,
        "supported_capabilities",
        frozenset({"pricing", "inventory", "listing"}),
    )
    listing_policy = ListingPolicyConfig(
        policy_version="listing-policy-v1",
        supports_publication="listing" in supported_capabilities,
    )
    publish_listing_handler = _ScopedWriteHandler(
        session=adapter_session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        factory=lambda actor: PublishListingHandler(
            actor=actor,
            session=adapter_session,
            gateway=listing_gateway,
            policy_config=listing_policy,
        ),
    )
    unpublish_listing_handler = _ScopedWriteHandler(
        session=adapter_session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        factory=lambda actor: UnpublishListingHandler(
            actor=actor,
            session=adapter_session,
            gateway=listing_gateway,
            policy_config=listing_policy,
        ),
    )

    def price_policy_factory(context: PreToolProfileContext) -> PricePreToolPolicy:
        """写 Tool 的业务策略工厂:绑定本次请求的 Session/Actor/ToolCall。"""
        return PricePreToolPolicy(
            session=context.session,
            config=pricing_policy.config,
            actor=context.actor,
            tool_call_id=context.request.tool_call_id,
            policy_revision=pricing_policy.version,
        )

    def legacy_registration_for(key: str) -> PreToolProfileRegistration:
        """第一版 Tool 的兼容 PreTool Profile。"""
        if key == STABLE_KEY_UPDATE_PRICE:
            return product_registration(
                fields=TOOL_ARGUMENT_FIELDS["update_price"],
                policy_mode="BUSINESS",
                resource_mode="RESOURCE",
                business_policy_factory=price_policy_factory,
                resource_resolver=StoreResourceResolver(),
            )
        if key == LOAD_SKILL_KEY:
            return runtime_control_registration()
        if key == LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key:
            return product_registration(
                fields=TOOL_ARGUMENT_FIELDS["get_product_snapshot"],
                policy_mode="NONE",
                resource_mode="RESOURCE",
                resource_resolver=StoreResourceResolver(),
            )
        if key == LEGACY_COMPARE_LISTING_PRICES_DEFINITION.stable_key:
            return product_registration(
                fields=TOOL_ARGUMENT_FIELDS["compare_listing_prices"],
                policy_mode="NONE",
                resource_mode="RESOURCE",
                resource_resolver=StoreIdsResourceResolver(),
            )
        return product_registration(
            fields=TOOL_ARGUMENT_FIELDS[LEGACY_SEARCH_PRODUCTS_DEFINITION.stable_key],
            policy_mode="NONE",
            resource_mode="WORKSPACE",
        )

    def current_registration_for(key: str) -> PreToolProfileRegistration:
        """P1 Tool 的 Profile；业务终态校验由对应领域 Handler 再次执行。"""
        if key == LOAD_SKILL_KEY:
            return runtime_control_registration()
        if key in {
            SET_LISTING_PRICE_DEFINITION.stable_key,
            ADJUST_INVENTORY_QUANTITY_DEFINITION.stable_key,
            PUBLISH_LISTING_DEFINITION.stable_key,
            UNPUBLISH_LISTING_DEFINITION.stable_key,
        }:
            return product_registration(
                fields=p1_fields_for(key),
                policy_mode="BUSINESS",
                resource_mode="RESOURCE",
            )
        return product_registration(
            fields=p1_fields_for(key),
            policy_mode="NONE",
            resource_mode="WORKSPACE",
        )

    write_execution_profile = ToolExecutionProfile(
        adapter_key=adapter_key,
        supports_reconciliation=True,
    )

    # 每个 Tool 显式映射到自己的 Handler:不再按 stable_key 在运行时 if/elif
    # 分发,新增读 Tool 只需新增一个 Handler 并在此登记。
    read_session_factory = lambda: AsyncSession(engine, expire_on_commit=False)
    current_handlers: dict[str, Any] = {
        SEARCH_PRODUCTS_DEFINITION.stable_key: _P1ReadHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
            invoke=_search_products_read,
        ),
        GET_LISTING_DEFINITION.stable_key: _P1ReadHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
            invoke=_get_listing_read,
        ),
        COMPARE_LISTING_PRICES_DEFINITION.stable_key: _P1ReadHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
            invoke=_compare_listing_prices_read,
        ),
        SET_LISTING_PRICE_DEFINITION.stable_key: set_listing_price_handler,
        GET_INVENTORY_LEVEL_DEFINITION.stable_key: _P1ReadHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
            invoke=_get_inventory_level_read,
        ),
        ADJUST_INVENTORY_QUANTITY_DEFINITION.stable_key: adjust_inventory_handler,
        PUBLISH_LISTING_DEFINITION.stable_key: publish_listing_handler,
        UNPUBLISH_LISTING_DEFINITION.stable_key: unpublish_listing_handler,
        LOAD_SKILL_KEY: LOAD_SKILL_HANDLER,
    }
    legacy_handlers: dict[str, Any] = {
        LEGACY_SEARCH_PRODUCTS_DEFINITION.stable_key: SearchProductsHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
        ),
        LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key: GetProductSnapshotHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
        ),
        LEGACY_COMPARE_LISTING_PRICES_DEFINITION.stable_key: CompareListingPricesHandler(
            session_factory=read_session_factory,
            workspace_id=workspace_id,
            actor_id=actor_id,
        ),
        STABLE_KEY_UPDATE_PRICE: write_handler,
        LOAD_SKILL_KEY: LOAD_SKILL_HANDLER,
    }

    tool_registry = ToolRegistry()
    for key, definition in product_tool_definitions():
        handler = current_handlers[key]
        is_write = definition.effect is ToolEffectKind.WRITE
        record = ToolVersionRecord(
            stable_key=key,
            version_id=capabilities.tool_version_ids[key],
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
        tool_registry.register(record, make_current=True)
        tool_registry.bind(
            record.version_id,
            ToolBinding(
                definition=definition,
                handler=handler,
                pretool_registration=current_registration_for(key),
                execution_profile=write_execution_profile if is_write else None,
                write_adapter_capabilities=(
                    _WRITE_ADAPTER_CAPABILITIES if is_write else frozenset()
                ),
            ),
        )
    for key, definition in legacy_product_tool_definitions():
        version_ids = capabilities.compatibility_tool_version_ids.get(key, ())
        if not version_ids:
            continue
        version_id = version_ids[0]
        is_write = definition.effect is ToolEffectKind.WRITE
        record = ToolVersionRecord(
            stable_key=key,
            version_id=version_id,
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
        tool_registry.register(record, make_current=False)
        tool_registry.bind(
            version_id,
            ToolBinding(
                definition=definition,
                handler=legacy_handlers[key],
                pretool_registration=legacy_registration_for(key),
                execution_profile=write_execution_profile if is_write else None,
                write_adapter_capabilities=(
                    _WRITE_ADAPTER_CAPABILITIES if is_write else frozenset()
                ),
            ),
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

    return ProductApplication(
        engine=engine,
        assembly=assembly,
        tool_registry=tool_registry,
        pricing_policy=pricing_policy,
        backend=backend,
        adapter=adapter,
        adapter_session=adapter_session,
        write_handler=write_handler,
        workspace_id=workspace_id,
        actor_id=actor_id,
        membership_id=membership_id,
        capabilities=capabilities,
        worker_id=worker_id,
    )


# ---------------------------------------------------------------------------
# 共享查询辅助
# ---------------------------------------------------------------------------


async def _resolve_listing(
    session: AsyncSession, workspace_id: uuid.UUID, store_id: uuid.UUID, sku: str
) -> Listing | None:
    """按 (workspace, store, sku) 读取 Listing(权威回读,强制刷新 ORM 状态)。

    与 ``agents.product_operations.policy.price_pretool.resolve_listing`` 的
    差别是这里带 ``populate_existing``:用于 Adapter 写入后的权威价格回读,
    必须丢弃会话缓存。Policy 侧读取无此需求。
    """
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


async def _next_message_sequence(session: AsyncSession, session_id: uuid.UUID) -> int:
    """会话内消息序号:max+1(uq_messages_session_sequence)。"""
    current = await session.scalar(
        select(func.max(Message.sequence_no)).where(Message.session_id == session_id)
    )
    return 1 if current is None else int(current) + 1


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


__all__ = [
    "ProductApplication",
    "RunView",
    "ToolCallSnapshot",
    "build_product_application",
]
