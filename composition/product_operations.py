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

模块归属:本组合核心同时被 API 宿主(:mod:`apps.api.executor_host`)与
eval(Eval Runner,计划 05 Task 2)复用,而 eval 不得 import apps(方向:
eval → runtime/agents/adapters)。本模块原本位于 Worker 组合根;由于 runtime
不 import agents/adapters、agents 不 import adapters(架构文档 §2),而
adapters 已依赖 agents(:mod:`adapters.commerce.mock_adapter` 先例),故
下沉到本模块作为单一事实源;:mod:`apps.assembly` 保留为兼容导出。

``ProductApplication`` 门面供驱动方(执行器宿主、测试、CLI、Eval)使用:
``start`` 落 Run(QUEUED)后唤醒执行器推进;``answer`` / ``confirm`` 用
Input/Confirmation 服务处理挂起请求后恢复推进;``recover`` 对响应丢失的写
操作回查后恢复推进。``advance`` / ``reconcile`` 实现执行器的 ``RunDriver``
协议;进程重启后的"已回答字段"由调用方从 InputRequest 持久化重建后传入。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.protocol import (
    CommerceAdapter,
)
from agents.product_operations.agent import (
    AGENT_KEY,
    AGENT_VERSION,
    LEGACY_AGENT_KEY,
    LEGACY_AGENT_VERSION,
    LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION,
    PRODUCT_AGENT_DEFINITION,
)
from agents.product_operations.inventory.gateway import InventoryGateway
from agents.product_operations.inventory.policy import InventoryPolicyConfig
from agents.product_operations.listing.gateway import ListingGateway
from agents.product_operations.listing.policy import ListingPolicyConfig
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.pricing.configuration import (
    PRICING_POLICY_STABLE_KEY,
    PublishedPricingPolicy,
)
from agents.product_operations.pricing.gateway import PricingGateway
from agents.product_operations.prompt import PRODUCT_AGENT_PROMPT
from agents.product_operations.skill import (
    LEGACY_PRODUCT_MAINTENANCE_SKILL,
    LEGACY_SKILL_VERSION,
    PRODUCT_MAINTENANCE_SKILL,
    PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS,
    SKILL_KEY,
    SKILL_VERSION,
)
from agents.product_operations.tools.update_price import (
    UpdatePriceToolHandler,
)
from composition.product_operations_bindings import (
    LegacyCommerceGateway,
    ProductOperationsBindingDependencies,
    build_product_tool_bindings,
)
from composition.product_operations_recovery import ProductOperationsRecovery
from composition.provisioning.product_operations import (
    ProductOperationsCapabilities,
    load_published_pricing_policy,
    product_tool_definitions,
    require_published_capabilities,
)
from runtime.access.actor import ActorContextBuilder
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
    ToolRegistry,
    ToolVersionRecord,
)
from runtime.capabilities.run_factory import RunFactory
from runtime.capabilities.validation import compute_schema_hash
from runtime.commands.session import SessionCommandService
from runtime.execution.executor import AdvanceOutcome, RunClaim, RunExecutor
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
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interfaces.state import RunStatus
from runtime.interfaces.tools import ToolEffect as ToolEffectKind
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
    ToolEffect,
)
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.identity import Workspace as WorkspaceRow
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
)
from runtime.persistence.repositories.run_snapshots import PolicySnapshotPin
from runtime.persistence.repositories.runs import RunRepository


ADAPTER_KEY: Final = "mock-commerce"
BACKEND_KEY: Final = "product-scripted"

_WRITE_ADAPTER_CAPABILITIES: Final[frozenset[str]] = frozenset({"operation", "reconciliation"})

_MAX_RECONCILE_ROUNDS: Final = 5

_MAX_ADVANCE_STEPS: Final = 8
"""请求内推进的步数上限:回查消除不确定性、NOT_APPLIED 重试都会把 Run 退回
QUEUED 再续推,单次请求最多连续推进这么多轮,避免病态场景占满请求。"""


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
        recovery: ProductOperationsRecovery,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        membership_id: uuid.UUID,
        capabilities: ProductOperationsCapabilities,
        executor_id: str,
    ) -> None:
        self._engine = engine
        self._assembly = assembly
        self._tool_registry = tool_registry
        self._pricing_policy = pricing_policy
        self._backend = backend
        self._adapter = adapter
        self._adapter_session = adapter_session
        self._write_handler = write_handler
        self._recovery = recovery
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
        )
        self._executor_id = executor_id
        # 门面自用的同步推进器:不 start()(不取单写者锁、不起后台循环),
        # 只复用 RunExecutor 的 CAS 领取与 Attempt 记账,供请求内同步推进使用。
        self._executor = RunExecutor(
            session_factory=self._session_factory,
            driver=self,
            instance_id=executor_id,
            lock=None,
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
        """创建 Run、落用户消息,并推进到新持久边界。"""
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
            await self._push(run_id)
        return await self.run_view(run_id)

    async def resume(self, run_id: uuid.UUID) -> RunView:
        """继续推进一个仍停在 QUEUED 的 Run(同步调用方驱动用)。

        请求内推进的步数预算耗尽、或回查/重试把 Run 退回 QUEUED 时,生产环境
        靠执行器后台兜底循环捡起来;不跑后台循环的同步调用方(评测)用这个方法
        按同一语义再推一轮:仍在 QUEUED 才推,已经到别的边界就直接返回。
        """
        view = await self.run_view(run_id)
        if view.status is RunStatus.QUEUED:
            await self._push(run_id)
        return await self.run_view(run_id)

    # -- RunDriver(进程内执行器注入的推进实现)--------------------------
    async def advance(
        self,
        claim: RunClaim,
        *,
        answered: Mapping[str, str] | None = None,
    ) -> AdvanceOutcome:
        """执行器已把 Run 置为 RUNNING;在本次调用内推进到下一个边界。

        ``answered`` 由调用方按持久化状态重建(进程重启后内存视图为空);
        未提供时回落到本次门面生命周期内累计的回答。
        """
        self._scope.begin(
            _ScopeContext(
                workspace_id=claim.workspace_id,
                run_id=claim.run_id,
                actor_id=self._actor_id,
                answered=dict(answered if answered is not None else self._answered),
            )
        )
        try:
            return await self._runtime.advance(claim)
        finally:
            self._scope.end()

    async def reconcile(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        """回查该 Run 的不确定 Operation;返回是否可以继续推进。"""
        del workspace_id  # 门面已绑定工作区
        return await self._recovery.reconcile_once(run_id)

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
        """请求内同步推进一个 Run(CAS 领取 → AgentLoop → 下一个边界)。

        写入 UNKNOWN 后的回查、NOT_APPLIED 重试都会把 Run 退回 QUEUED;这类
        续推必须在本请求内完成,否则调用方只能看到中途的 QUEUED,而真正推进
        依赖后台循环的下一次扫描。步数上限防止病态场景把请求占满。
        """
        for _ in range(_MAX_ADVANCE_STEPS):
            outcome = await self._executor.advance_now(run_id)
            if outcome is None or outcome.next_step is None:
                return

    async def _reconcile_once(self, run_id: uuid.UUID) -> bool:
        """委托模块化恢复服务;主组合根不解释领域 ``expected_state``。"""
        return await self._recovery.reconcile_once(run_id)


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
    executor_id: str = "product-executor",
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
    for version_id in capabilities.compatibility_agent_version_ids:
        agent_id = capabilities.compatibility_agent_ids.get(version_id)
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
        gateway=LegacyCommerceGateway(adapter),
        policy_version=pricing_policy.config.policy_version,
        policy_version_id=pricing_policy.version_id,
        policy_revision=pricing_policy.version,
        policy_config_hash=pricing_policy.config_hash,
    )
    pricing_gateway = cast(PricingGateway, adapter)
    inventory_gateway = cast(InventoryGateway, adapter)
    listing_gateway = cast(ListingGateway, adapter)
    supported_capabilities = getattr(
        adapter,
        "supported_capabilities",
        frozenset({"pricing", "inventory", "listing"}),
    )
    read_session_factory = lambda: AsyncSession(engine, expire_on_commit=False)
    tool_registry = ToolRegistry()
    binding_dependencies = ProductOperationsBindingDependencies(
        session_factory=read_session_factory,
        workspace_id=workspace_id,
        actor_id=actor_id,
        adapter_session=adapter_session,
        pricing_gateway=pricing_gateway,
        inventory_gateway=inventory_gateway,
        listing_gateway=listing_gateway,
        pricing_policy=pricing_policy,
        inventory_policy=InventoryPolicyConfig(policy_version="inventory-policy-v1"),
        listing_policy=ListingPolicyConfig(
            policy_version="listing-policy-v1",
            supports_publication="listing" in supported_capabilities,
        ),
        capabilities=capabilities,
        adapter_key=adapter_key,
        legacy_update_handler=write_handler,
    )
    for versioned in build_product_tool_bindings(binding_dependencies):
        definition = versioned.binding.definition
        record = ToolVersionRecord(
            stable_key=definition.stable_key,
            version_id=versioned.version_id,
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
        tool_registry.register(record, make_current=versioned.make_current)
        tool_registry.bind(versioned.version_id, versioned.binding)

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
    recovery = ProductOperationsRecovery(
        session_factory=read_session_factory,
        workspace_id=workspace_id,
        actor_id=actor_id,
        pricing_gateway=pricing_gateway,
        inventory_gateway=inventory_gateway,
        listing_gateway=listing_gateway,
        run_limits=RunLimits.from_definition(PRODUCT_AGENT_DEFINITION.loop_limits),
        before_external_call=adapter_session.expire_all,
    )

    return ProductApplication(
        engine=engine,
        assembly=assembly,
        tool_registry=tool_registry,
        pricing_policy=pricing_policy,
        backend=backend,
        adapter=adapter,
        adapter_session=adapter_session,
        write_handler=write_handler,
        recovery=recovery,
        workspace_id=workspace_id,
        actor_id=actor_id,
        membership_id=membership_id,
        capabilities=capabilities,
        executor_id=executor_id,
    )


async def _resolve_listing(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    store_id: uuid.UUID,
    sku: str,
) -> Listing | None:
    """权威回读 Listing，强制刷新可能被 Adapter 改写的 ORM 状态。"""
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
__all__ = [
    "ProductApplication",
    "RunView",
    "ToolCallSnapshot",
    "build_product_application",
]
