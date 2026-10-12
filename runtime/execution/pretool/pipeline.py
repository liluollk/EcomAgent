"""固定 PreToolUse 管线(计划 02 Task 6;架构文档 §9)。

阶段顺序固定为 ``PRETOOL_STAGE_ORDER`` 的 10 个阶段,装配时重排或缺失即拒
绝;任一阶段返回决策立即短路,后续阶段不再执行,DENY 不生成 Input Request。

各阶段职责与注入依赖:

- tool_resolution:经 :class:`RuntimeToolRegistry` 解析 Run 钉住的 Tool 版本,
  未知 Tool / 版本漂移 / 禁用版本直接拒绝;
- schema_validation::class:`RuntimeSchemaValidator` 完成 JSON 解析、根结构、
  未允许字段与必填字段校验(缺失字段生成 SchemaDiagnostic 留给后续阶段);
- input_normalization:注入 :class:`InputNormalizer`(业务 Agent 注册的确定性
  归一化,不默认值隐藏缺失字段);
- argument_provenance:注入 :class:`ArgumentProvenanceResolver`(确定性规则,
  不接受模型自报来源);
- input_requirement:注入 :class:`InputRequirementResolver`,把 SchemaDiagnostic
  转成 InputRequirement,或判为不可补充;
- authorization_resource:注入 :class:`AuthorizationResourceResolver`,从规范化
  参数提取 ``(resource_type, resource_id)``,不读写授权结果;
- access_control:注入 :class:`Authorizer`,按 ToolDefinition 动作权限与资源
  引用统一授权,不接受模型自报权限;
- business_policy:注入 :class:`BusinessPolicy`,只返回 :class:`PolicyDecision`
  值对象,不获取 Repository;
- execution_mode:注入 :class:`ExecutionModeResolver`(缺省为确定性的
  READ→READ_ONLY、WRITE→CONFIRM 规则),决定是否拒绝写入或需要确认;
- confirmation_verification:注入 :class:`AcceptedConfirmationLookup` 查询回调
  (指纹、Actor、过期与状态校验由交互阶段实现),无已接受确认的写操作返回
  REQUIRE_CONFIRMATION(携带 ConfirmationSpec)。

业务扩展只返回值对象;只有 Runtime 应用服务执行状态迁移并写 AgentEvent。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from runtime.access.actor import ActorContext
from runtime.access.authorization import Authorizer
from runtime.execution.pretool.decisions import (
    ACCESS_DENIED,
    ARGUMENTS_INCOMPLETE,
    EXECUTION_MODE_READ_ONLY,
    INVALID_MODEL_OUTPUT,
    PIPELINE_INVARIANT,
    POLICY_DENIED,
    PRETOOL_STAGE_ORDER,
    STAGE_ACCESS_CONTROL,
    STAGE_ARGUMENT_PROVENANCE,
    STAGE_AUTHORIZATION_RESOURCE,
    STAGE_BUSINESS_POLICY,
    STAGE_CONFIRMATION_VERIFICATION,
    STAGE_EXECUTION_MODE,
    STAGE_INPUT_NORMALIZATION,
    STAGE_INPUT_REQUIREMENT,
    STAGE_SCHEMA_VALIDATION,
    STAGE_TOOL_RESOLUTION,
    TOOL_DISABLED,
    TOOL_PRETOOL_PROFILE_MISSING,
    TOOL_VERSION_MISMATCH,
    UNKNOWN_TOOL,
    ArgumentProvenance,
    ExecutionModeDecision,
    PolicyDecision,
    PreToolDecision,
    SchemaDiagnostic,
    allow,
    deny,
    require_confirmation,
    require_input,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.pretool.profiles import (
    PreToolProfile,
    PreToolProfileContext,
)
from runtime.execution.tool_registry import (
    TOOL_RESOLVED as TOOL_STATUS_RESOLVED,
)
from runtime.execution.tool_registry import (
    TOOL_UNKNOWN as TOOL_STATUS_UNKNOWN,
)
from runtime.execution.tool_registry import (
    TOOL_VERSION_MISMATCH as TOOL_STATUS_VERSION_MISMATCH,
)
from runtime.execution.tool_registry import (
    RuntimeToolRegistry,
    ToolResolution,
)
from runtime.interfaces.identifiers import ToolCallId
from runtime.interfaces.requests import ConfirmationSpec, InputRequirement
from runtime.interfaces.tools import ToolEffect


class ToolCallRequest(BaseModel):
    """模型发起、进入 PreToolUse 管线的单个 ToolCall(架构文档 §8)。

    与 ``runtime.capabilities.write_service`` 的同名过渡形态不同:此处参数以
    未解析 JSON 文本进入 schema_validation 阶段,并携带模型引用的 Tool 版本
    号用于版本漂移检测。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: ToolCallId
    stable_key: str
    version: int
    raw_arguments: str


ARGUMENT_UNTRUSTED: Final = "UNTRUSTED"
"""来源守卫判定的不可信来源标识(与 ArgumentTrustLevel.UNTRUSTED 同值)。"""

PROVENANCE_GUARD_MESSAGE: Final = (
    "key arguments lack trusted user or authoritative evidence"
)
"""不可信关键字段并入 SchemaDiagnostic 时的诊断消息。"""


@dataclass
class PreToolContext:
    """管线内部逐阶段累积的上下文;DENY 短路后不再被触碰。"""

    request: ToolCallRequest
    actor: ActorContext
    tool_resolution: ToolResolution | None = None
    profile: PreToolProfile | None = None
    arguments: Mapping[str, Any] | None = None
    normalized_arguments: Mapping[str, Any] | None = None
    provenance: ArgumentProvenance | None = None
    schema_diagnostic: SchemaDiagnostic | None = None
    resources: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    resource_versions: Mapping[str, int] = field(default_factory=dict)
    execution_mode: ExecutionModeDecision | None = None


# ---- 注入协作对象的协议(业务扩展只返回值对象)----


class InputNormalizer(Protocol):
    """业务 Agent 注册的确定性归一化:别名、ID 格式、单位;不做模型猜测。"""

    async def normalize(
        self, *, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class ArgumentProvenanceResolver(Protocol):
    """根据当前用户消息、已回答 Request 与权威读取生成参数来源。"""

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> ArgumentProvenance: ...


class InputRequirementResolver(Protocol):
    """决定缺失字段能否转成用户可回答的 Input Request。"""

    async def resolve(
        self, *, diagnostic: SchemaDiagnostic
    ) -> InputRequirement | None: ...


class AuthorizationResourceResolver(Protocol):
    """从规范化参数提取 ``(resource_type, resource_id)`` 引用集合。"""

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> frozenset[tuple[str, str]]: ...


class BusinessPolicy(Protocol):
    """业务策略:只返回 PolicyDecision 值对象,不获取 Repository。"""

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision: ...


class ExecutionModeResolver(Protocol):
    """决定只读、确认或自动执行。"""

    async def __call__(self, *, effect: ToolEffect) -> ExecutionModeDecision: ...


class AcceptedConfirmationLookup(Protocol):
    """已接受 Confirmation 的查询回调;指纹/Actor/过期/状态校验由实现方负责。"""

    async def __call__(
        self, *, tool_call_id: UUID, stable_key: str, actor_id: UUID
    ) -> bool: ...


@dataclass(frozen=True)
class PreToolDependencies:
    """管线只保留 Runtime 统一依赖;业务扩展改由每个 Tool 的 Profile 提供。

    阶段协作对象(归一化、来源、缺失字段、资源、业务策略)不再从组合根全局
    注入,而在 ``tool_resolution`` 成功后,由当前 ToolBinding 的
    :class:`PreToolProfileRegistration` 用 ``profile_context`` 延迟创建。
    """

    tool_registry: RuntimeToolRegistry
    schema_validator: RuntimeSchemaValidator
    authorizer: Authorizer
    execution_mode_resolver: ExecutionModeResolver | None = None
    accepted_confirmation_lookup: AcceptedConfirmationLookup | None = None
    profile_context: PreToolProfileContext | None = None


StageHandler = Callable[[PreToolContext], Awaitable[PreToolDecision | None]]


@dataclass(frozen=True)
class NamedStage:
    """一个管线阶段:固定名 + 异步 handler;返回决策即短路。"""

    name: str
    handler: StageHandler


class PipelineConfigurationError(RuntimeError):
    """阶段序列与固定顺序不符;管线拒绝装配。"""


# ---- 默认阶段实现 ----


def _tool_resolution_stage(dependencies: PreToolDependencies) -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        resolution = dependencies.tool_registry.resolve(
            stable_key=context.request.stable_key,
            version=context.request.version,
        )
        context.tool_resolution = resolution
        if resolution.status == TOOL_STATUS_UNKNOWN:
            return deny(UNKNOWN_TOOL, safe_message="tool is not pinned for this run")
        if resolution.status == TOOL_STATUS_VERSION_MISMATCH:
            return deny(
                TOOL_VERSION_MISMATCH,
                safe_message="tool version referenced by the tool call does not match the pinned version",
            )
        if resolution.status != TOOL_STATUS_RESOLVED:
            return deny(TOOL_DISABLED, safe_message="tool version is disabled")
        # 解析成功后,用当前 ToolBinding 的注册描述创建本次评估的 Profile;
        # 其中绑定与上下文都只在该次 PreToolUse 内有效。
        registration = (
            resolution.binding.pretool_registration if resolution.binding else None
        )
        if registration is None:
            return deny(
                TOOL_PRETOOL_PROFILE_MISSING,
                safe_message="resolved tool binding has no pretool profile registered",
            )
        context.profile = await registration.factory.create(dependencies.profile_context)
        return None

    return handler


def _schema_validation_stage(validator: RuntimeSchemaValidator) -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        record = context.tool_resolution.record if context.tool_resolution else None
        if record is None:
            return deny(PIPELINE_INVARIANT, safe_message="tool resolution produced no record")
        outcome = validator.validate(
            raw_arguments=context.request.raw_arguments,
            input_schema=record.input_schema,
        )
        if outcome.status == "INVALID_MODEL_OUTPUT":
            return deny(INVALID_MODEL_OUTPUT, safe_message=outcome.message)
        context.arguments = outcome.arguments
        if outcome.diagnostic is not None:
            context.schema_diagnostic = outcome.diagnostic
        return None

    return handler


def _require_profile(context: PreToolContext) -> PreToolProfile | None:
    """返回本次评估的 Profile;缺失表示 tool_resolution 未被正确执行。"""
    return context.profile


def _input_normalization_stage() -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        profile = _require_profile(context)
        if profile is None:
            return deny(
                PIPELINE_INVARIANT, safe_message="pretool profile was not resolved"
            )
        arguments = context.arguments or {}
        context.normalized_arguments = await profile.input_normalizer.normalize(
            arguments=arguments
        )
        return None

    return handler


def _argument_provenance_stage() -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        profile = _require_profile(context)
        if profile is None:
            return deny(
                PIPELINE_INVARIANT, safe_message="pretool profile was not resolved"
            )
        arguments = context.normalized_arguments or {}
        provenance = await profile.argument_provenance_resolver.resolve(
            arguments=arguments
        )
        context.provenance = provenance
        _apply_provenance_guard(context, profile, provenance, arguments)
        return None

    return handler


def _apply_provenance_guard(
    context: PreToolContext,
    profile: PreToolProfile,
    provenance: ArgumentProvenance,
    arguments: Mapping[str, Any],
) -> None:
    """来源守卫(架构文档 §5):已提供但不可信的关键字段 → 校验诊断。

    关键字段由 Tool 自己的 Profile 声明;守卫把命中字段并入 SchemaDiagnostic,
    交由 input_requirement 阶段转成可回答的 Input Request,不进入写执行阶段。
    """
    guard_fields = profile.provenance_guard_fields
    if not guard_fields:
        return
    untrusted = tuple(
        field
        for field in guard_fields
        if field in arguments
        and provenance.field_sources.get(field) == ARGUMENT_UNTRUSTED
    )
    if not untrusted:
        return
    diagnostic = context.schema_diagnostic
    merged_paths = tuple(
        dict.fromkeys(
            (*(diagnostic.field_paths if diagnostic is not None else ()), *untrusted)
        )
    )
    context.schema_diagnostic = SchemaDiagnostic(
        field_paths=merged_paths,
        message=PROVENANCE_GUARD_MESSAGE,
    )


def _input_requirement_stage() -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        diagnostic = context.schema_diagnostic
        if diagnostic is None:
            return None
        profile = _require_profile(context)
        if profile is None:
            return deny(
                PIPELINE_INVARIANT, safe_message="pretool profile was not resolved"
            )
        requirement = await profile.input_requirement_resolver.resolve(
            diagnostic=diagnostic
        )
        if requirement is None:
            return deny(
                ARGUMENTS_INCOMPLETE,
                safe_message="missing business fields cannot be turned into an input request",
            )
        return require_input(input_requirement=requirement, schema_diagnostic=diagnostic)

    return handler


def _authorization_resource_stage() -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        profile = _require_profile(context)
        if profile is None:
            return deny(
                PIPELINE_INVARIANT, safe_message="pretool profile was not resolved"
            )
        arguments = context.normalized_arguments or {}
        context.resources = await profile.authorization_resource_resolver.resolve(
            arguments=arguments
        )
        return None

    return handler


def _access_control_stage(dependencies: PreToolDependencies) -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        record = context.tool_resolution.record if context.tool_resolution else None
        if record is None:
            return deny(PIPELINE_INVARIANT, safe_message="tool resolution produced no record")
        decision = dependencies.authorizer.authorize(
            context.actor,
            required_permissions=record.required_permissions,
            resources=context.resources,
        )
        if not decision.allowed:
            return deny(
                decision.reason_code or ACCESS_DENIED,
                safe_message="actor lacks permission for this tool or resource",
            )
        return None

    return handler


def _business_policy_stage() -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        profile = _require_profile(context)
        if profile is None:
            return deny(
                PIPELINE_INVARIANT, safe_message="pretool profile was not resolved"
            )
        record = context.tool_resolution.record if context.tool_resolution else None
        if record is None:
            return deny(PIPELINE_INVARIANT, safe_message="tool resolution produced no record")
        policy_decision = await profile.business_policy.evaluate(
            stable_key=record.stable_key,
            arguments=context.normalized_arguments or {},
            resources=context.resources,
        )
        if policy_decision.allowed:
            context.resource_versions = dict(policy_decision.resource_versions)
            return None
        return deny(
            policy_decision.reason_code or POLICY_DENIED,
            safe_message=policy_decision.safe_message,
            policy_decision=policy_decision,
        )

    return handler


async def _default_execution_mode_resolver(*, effect: ToolEffect) -> ExecutionModeDecision:
    """缺省确定性规则:只读 Tool 直接执行;写 Tool 必须确认。"""
    if effect == ToolEffect.WRITE:
        return ExecutionModeDecision(mode="CONFIRM")
    return ExecutionModeDecision(mode="READ_ONLY")


def _execution_mode_stage(dependencies: PreToolDependencies) -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        record = context.tool_resolution.record if context.tool_resolution else None
        if record is None or record.effect is None:
            return deny(PIPELINE_INVARIANT, safe_message="resolved tool version is unclassified")
        resolver = dependencies.execution_mode_resolver or _default_execution_mode_resolver
        context.execution_mode = await resolver(effect=record.effect)
        if context.execution_mode.mode == "DENY":
            return deny(
                EXECUTION_MODE_READ_ONLY,
                safe_message="workspace execution mode does not allow write operations",
            )
        return None

    return handler


def _confirmation_verification_stage(dependencies: PreToolDependencies) -> StageHandler:
    async def handler(context: PreToolContext) -> PreToolDecision | None:
        mode = context.execution_mode
        if mode is None:
            return deny(PIPELINE_INVARIANT, safe_message="execution mode was not decided")
        if mode.mode != "CONFIRM":
            return None
        lookup = dependencies.accepted_confirmation_lookup
        accepted = (
            await lookup(
                tool_call_id=context.request.tool_call_id,
                stable_key=context.request.stable_key,
                actor_id=context.actor.actor_id,
            )
            if lookup is not None
            else False
        )
        if accepted:
            return None
        return require_confirmation(
            confirmation_spec=ConfirmationSpec(
                resource_versions=dict(context.resource_versions)
            )
        )

    return handler


def default_stages(dependencies: PreToolDependencies) -> tuple[NamedStage, ...]:
    """按固定顺序装配 10 个默认阶段;顺序由此函数确定,调用方不得重排。"""
    return (
        NamedStage(
            name=STAGE_TOOL_RESOLUTION,
            handler=_tool_resolution_stage(dependencies),
        ),
        NamedStage(
            name=STAGE_SCHEMA_VALIDATION,
            handler=_schema_validation_stage(dependencies.schema_validator),
        ),
        NamedStage(
            name=STAGE_INPUT_NORMALIZATION,
            handler=_input_normalization_stage(),
        ),
        NamedStage(
            name=STAGE_ARGUMENT_PROVENANCE,
            handler=_argument_provenance_stage(),
        ),
        NamedStage(
            name=STAGE_INPUT_REQUIREMENT,
            handler=_input_requirement_stage(),
        ),
        NamedStage(
            name=STAGE_AUTHORIZATION_RESOURCE,
            handler=_authorization_resource_stage(),
        ),
        NamedStage(
            name=STAGE_ACCESS_CONTROL,
            handler=_access_control_stage(dependencies),
        ),
        NamedStage(
            name=STAGE_BUSINESS_POLICY,
            handler=_business_policy_stage(),
        ),
        NamedStage(
            name=STAGE_EXECUTION_MODE,
            handler=_execution_mode_stage(dependencies),
        ),
        NamedStage(
            name=STAGE_CONFIRMATION_VERIFICATION,
            handler=_confirmation_verification_stage(dependencies),
        ),
    )


class PreToolUsePipeline:
    """固定 10 阶段 PreToolUse 管线(架构文档 §9)。

    - 阶段序列必须与 ``PRETOOL_STAGE_ORDER`` 完全一致,重排或缺失在构造期
      即拒绝(不可重排);
    - 任一阶段返回决策立即短路并作为 evaluate 结果;DENY 不生成 Input Request;
    - 全部阶段通过返回 ``ALLOW``。
    """

    def __init__(self, *, actor: ActorContext, stages: Sequence[NamedStage]) -> None:
        names = [stage.name for stage in stages]
        if names != list(PRETOOL_STAGE_ORDER):
            raise PipelineConfigurationError(
                "PreToolUse 阶段顺序固定,不可重排或缺省: "
                + " -> ".join(PRETOOL_STAGE_ORDER)
            )
        self._actor = actor
        self._stages = tuple(stages)

    async def evaluate(self, tool_call: ToolCallRequest) -> PreToolDecision:
        """对单个 ToolCall 顺序执行全部阶段,返回首个决策或 ALLOW。"""
        context = PreToolContext(request=tool_call, actor=self._actor)
        for stage in self._stages:
            decision = await stage.handler(context)
            if decision is not None:
                return self._with_normalized_arguments(decision, context)
        return self._with_normalized_arguments(allow(), context)

    @staticmethod
    def _with_normalized_arguments(
        decision: PreToolDecision, context: PreToolContext
    ) -> PreToolDecision:
        if context.normalized_arguments is None:
            return decision
        return decision.model_copy(
            update={"normalized_arguments": dict(context.normalized_arguments)}
        )


__all__ = [
    "AcceptedConfirmationLookup",
    "ArgumentProvenanceResolver",
    "AuthorizationResourceResolver",
    "BusinessPolicy",
    "ExecutionModeResolver",
    "InputNormalizer",
    "InputRequirementResolver",
    "NamedStage",
    "PipelineConfigurationError",
    "PreToolContext",
    "PreToolDependencies",
    "PreToolUsePipeline",
    "StageHandler",
    "ToolCallRequest",
    "default_stages",
]
