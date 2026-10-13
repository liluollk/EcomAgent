"""PreToolUse 管线行为测试(计划 02 Task 6;架构文档 §9)。

纯单元测试:阶段协作对象全部用测试内替身,不触数据库。覆盖:

- 固定 10 阶段顺序与 DENY 短路(recording pipeline);
- 模型输出质量问题(不可解析 / 根结构错误 / 未允许字段)→ DENY(INVALID_MODEL_OUTPUT),
  不生成 Input Request;
- 缺失业务字段 → REQUIRE_INPUT,携带 SchemaDiagnostic 与 field_paths;
- 授权两阶段(PERMISSION_DENIED / RESOURCE_SCOPE_DENIED)、业务策略、
  execution_mode 与 confirmation_verification 的决策语义;
- 决策对象为 frozen 值对象,含 kind/reason_code/detail。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from runtime.access.actor import ActorContext
from runtime.access.authorization import (
    PERMISSION_DENIED,
    RESOURCE_SCOPE_DENIED,
    Authorizer,
)
from runtime.capabilities.registry import (
    ToolBinding,
    ToolRegistry,
    ToolVersionRecord,
)
from runtime.execution.pretool.decisions import (
    CONFIRMATION_REQUIRED,
    INVALID_MODEL_OUTPUT,
    TOOL_VERSION_MISMATCH,
    UNKNOWN_TOOL,
    ExecutionModeDecision,
    PolicyDecision,
    PreToolDecision,
)
from runtime.execution.pretool.pipeline import (
    NamedStage,
    PreToolContext,
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.profiles import (
    EmptyProvenanceResolver,
    NoAuthorizationResources,
    NoBusinessPolicy,
    PassThroughInputNormalizer,
    PreToolProfile,
    PreToolProfileContext,
    PreToolProfileRegistration,
    UnresolvableInputRequirement,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.interfaces.identifiers import ResourceScope, ToolCallId
from runtime.interfaces.requests import ConfirmationSpec, InputRequirement
from runtime.interfaces.tools import ToolDefinition, ToolEffect
from runtime.persistence.base import MembershipRole


SEARCH_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}

PUBLISH_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "listing_id": {"type": "string"},
        "store_id": {"type": "string"},
    },
    "required": ["listing_id", "store_id"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 夹具辅助:工具注册、Actor、ToolCall 构造
# ---------------------------------------------------------------------------


def make_tool_record(
    *,
    stable_key: str,
    effect: ToolEffect,
    schema: dict[str, Any],
    permissions: frozenset[str],
) -> ToolVersionRecord:
    return ToolVersionRecord(
        stable_key=stable_key,
        version_id=uuid4(),
        version=1,
        source_kind="LOCAL",
        effect=effect,
        input_schema=schema,
        output_schema={"type": "object"},
        schema_hash=f"hash-{stable_key}",
        required_permissions=permissions,
    )


def _tool_definition(*, stable_key: str, effect: ToolEffect, schema: dict[str, Any],
                     permissions: frozenset[str]) -> ToolDefinition:
    return ToolDefinition(
        stable_key=stable_key,
        version=1,
        description=stable_key,
        input_json_schema=schema,
        output_json_schema={"type": "object"},
        effect=effect,
        required_permissions=permissions,
        source_kind="LOCAL",
        schema_hash=f"hash-{stable_key}",
    )


class StaticProfileFactory:
    """测试替身 Profile 工厂:无视请求级上下文,固定返回给定 Profile。"""

    def __init__(self, profile: PreToolProfile) -> None:
        self._profile = profile

    async def create(self, context: Any) -> PreToolProfile:
        del context
        return self._profile


def make_profile(**collaborators: Any) -> PreToolProfile:
    """用测试替身组装 Profile;未给出的协作对象使用显式空实现。"""
    return PreToolProfile(
        input_normalizer=collaborators.get(
            "input_normalizer", PassThroughInputNormalizer()
        ),
        argument_provenance_resolver=collaborators.get(
            "argument_provenance_resolver", EmptyProvenanceResolver()
        ),
        input_requirement_resolver=collaborators.get(
            "input_requirement_resolver", UnresolvableInputRequirement()
        ),
        authorization_resource_resolver=collaborators.get(
            "authorization_resource_resolver", NoAuthorizationResources()
        ),
        business_policy=collaborators.get("business_policy", NoBusinessPolicy()),
        provenance_guard_fields=tuple(collaborators.get("provenance_guard_fields", ())),
    )


def _bind(
    registry: ToolRegistry,
    *,
    stable_key: str,
    effect: ToolEffect,
    schema: dict[str, Any],
    permissions: frozenset[str],
    profile: PreToolProfile,
) -> None:
    is_write = effect is ToolEffect.WRITE
    record = registry.find_by_key(stable_key)
    assert record is not None
    registry.bind(
        record.version_id,
        ToolBinding(
            definition=_tool_definition(
                stable_key=stable_key, effect=effect, schema=schema,
                permissions=permissions,
            ),
            handler=object(),
            pretool_registration=PreToolProfileRegistration(
                factory=StaticProfileFactory(profile),
                policy_mode="BUSINESS" if is_write else "NONE",
                resource_mode="RESOURCE" if is_write else "WORKSPACE",
            ),
        )
    )


def make_registry(profile: PreToolProfile) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        make_tool_record(
            stable_key="listing.search",
            effect=ToolEffect.READ,
            schema=SEARCH_SCHEMA,
            permissions=frozenset({"listing:read"}),
        )
    )
    registry.register(
        make_tool_record(
            stable_key="listing.publish",
            effect=ToolEffect.WRITE,
            schema=PUBLISH_SCHEMA,
            permissions=frozenset({"listing:write"}),
        )
    )
    _bind(
        registry,
        stable_key="listing.search",
        effect=ToolEffect.READ,
        schema=SEARCH_SCHEMA,
        permissions=frozenset({"listing:read"}),
        profile=profile,
    )
    _bind(
        registry,
        stable_key="listing.publish",
        effect=ToolEffect.WRITE,
        schema=PUBLISH_SCHEMA,
        permissions=frozenset({"listing:write"}),
        profile=profile,
    )
    return registry


def make_actor(
    *,
    permissions: frozenset[str] = frozenset({"listing:read", "listing:write"}),
    listing_ids: tuple[str, ...] = ("l-1",),
) -> ActorContext:
    return ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        membership_id=uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=permissions,
        resource_scopes={
            "listing": ResourceScope(mode="SELECTED", resource_ids=frozenset(listing_ids)),
        },
        request_id="req-test",
    )


def tool_call(**overrides: Any) -> ToolCallRequest:
    values: dict[str, Any] = {
        "tool_call_id": ToolCallId(uuid4()),
        "stable_key": "listing.search",
        "version": 1,
        "raw_arguments": json.dumps({"query": "running shoes"}),
    }
    values.update(overrides)
    return ToolCallRequest(**values)


def valid_tool_call() -> ToolCallRequest:
    """可走完全部阶段的只读 ToolCall。"""
    return tool_call()


def write_tool_call() -> ToolCallRequest:
    return tool_call(
        stable_key="listing.publish",
        raw_arguments=json.dumps({"listing_id": "l-1", "store_id": "s-1"}),
    )


# ---------------------------------------------------------------------------
# 阶段协作替身(全部只返回值对象)
# ---------------------------------------------------------------------------


class RecordingInputRequirementResolver:
    """把 SchemaDiagnostic 转成 InputRequirement 的业务替身;记录收到的诊断。"""

    def __init__(self) -> None:
        self.received: Any = None

    async def resolve(
        self, *, diagnostic: Any, arguments: Mapping[str, Any]
    ) -> InputRequirement | None:
        del arguments
        self.received = diagnostic
        if diagnostic is None:
            return None
        return InputRequirement()


class BusinessInputRequirementResolver:
    """按规范化参数直接返回业务 InputRequirement 的替身(无 SchemaDiagnostic)。"""

    def __init__(self, requirement: InputRequirement) -> None:
        self._requirement = requirement
        self.received_arguments: Mapping[str, Any] | None = None

    async def resolve(
        self, *, diagnostic: Any, arguments: Mapping[str, Any]
    ) -> InputRequirement | None:
        assert diagnostic is None, "业务输入需求不应携带 SchemaDiagnostic"
        self.received_arguments = arguments
        return self._requirement


class ListingResourceResolver:
    """从规范化参数提取 (resource_type, resource_id) 的业务替身。"""

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> frozenset[tuple[str, str]]:
        return frozenset({("listing", str(arguments["listing_id"]))})


class DenyingPolicy:
    """业务策略替身:返回拒绝的 PolicyDecision 值对象。"""

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            reason_code="LISTING_FROZEN",
            safe_message="listing is frozen by policy",
        )


class AutoModeResolver:
    """execution_mode 替身:即使写 Tool 也判定为自动执行。"""

    async def __call__(self, *, effect: ToolEffect) -> ExecutionModeDecision:
        return ExecutionModeDecision(mode="AUTO")


class RevisionedPolicy:
    """业务策略替身:允许执行,并携带一个业务策略修订号。"""

    def __init__(self, revision: int) -> None:
        self.revision = revision

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        del stable_key, arguments, resources
        return PolicyDecision(allowed=True, policy_revision=self.revision)


class StaticConfirmationLookup:
    """已接受 Confirmation 查询回调替身。"""

    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.requested: dict[str, Any] = {}

    async def __call__(
        self,
        *,
        tool_call_id: UUID,
        stable_key: str,
        actor_id: UUID,
        policy_version: int | None,
    ) -> bool:
        self.requested = {
            "tool_call_id": tool_call_id,
            "stable_key": stable_key,
            "actor_id": actor_id,
            "policy_version": policy_version,
        }
        return self.accepted


def build_deps(profile: PreToolProfile, **collaborators: Any) -> PreToolDependencies:
    """构造 Runtime 统一依赖;业务协作对象由 ToolBinding 的 Profile 提供。

    ``StaticProfileFactory`` 无视请求级上下文,因此这里给它一个占位上下文,
    只用于通过 Runtime 的"缺上下文即 fail closed"装配校验。
    """
    return PreToolDependencies(
        tool_registry=RuntimeToolRegistry(make_registry(profile)),
        schema_validator=RuntimeSchemaValidator(),
        authorizer=Authorizer(),
        execution_mode_resolver=collaborators.get("execution_mode_resolver"),
        accepted_confirmation_lookup=collaborators.get("accepted_confirmation_lookup"),
        profile_context=PreToolProfileContext(
            session=None, actor=make_actor(), request=tool_call()
        ),
    )


def make_pipeline(*, actor: ActorContext | None = None, **overrides: Any) -> PreToolUsePipeline:
    profile = make_profile(**overrides)
    return PreToolUsePipeline(
        actor=actor or make_actor(),
        stages=default_stages(build_deps(profile, **overrides)),
    )


class RecordingPipeline:
    """转发 evaluate 并按执行顺序记录实际经过的阶段名。"""

    def __init__(self, pipeline: PreToolUsePipeline, calls: list[str]) -> None:
        self._pipeline = pipeline
        self.calls = calls

    async def evaluate(self, tool_call_request: ToolCallRequest) -> PreToolDecision:
        return await self._pipeline.evaluate(tool_call_request)


@pytest.fixture()
def pipeline() -> PreToolUsePipeline:
    return make_pipeline()


@pytest.fixture()
def recording_pipeline() -> RecordingPipeline:
    calls: list[str] = []
    stages: list[NamedStage] = []
    for stage in default_stages(build_deps(make_profile())):
        async def handler(
            context: PreToolContext,
            _inner: Any = stage.handler,
            _name: str = stage.name,
        ) -> PreToolDecision | None:
            calls.append(_name)
            return await _inner(context)

        stages.append(NamedStage(name=stage.name, handler=handler))
    return RecordingPipeline(PreToolUsePipeline(actor=make_actor(), stages=stages), calls)


# ---------------------------------------------------------------------------
# 计划原文测试
# ---------------------------------------------------------------------------


async def test_pretool_stages_have_fixed_order(recording_pipeline: RecordingPipeline) -> None:
    await recording_pipeline.evaluate(valid_tool_call())
    assert recording_pipeline.calls == [
        "tool_resolution",
        "schema_validation",
        "input_normalization",
        "argument_provenance",
        "input_requirement",
        "authorization_resource",
        "access_control",
        "business_policy",
        "execution_mode",
        "confirmation_verification",
    ]


async def test_unparseable_arguments_are_model_error(pipeline: PreToolUsePipeline) -> None:
    decision = await pipeline.evaluate(tool_call(raw_arguments="{"))
    assert decision.kind == "DENY"
    assert decision.reason_code == INVALID_MODEL_OUTPUT


# ---------------------------------------------------------------------------
# 全绿路径 / 未知 Tool / 版本漂移
# ---------------------------------------------------------------------------


async def test_green_path_read_tool_allows(pipeline: PreToolUsePipeline) -> None:
    decision = await pipeline.evaluate(valid_tool_call())
    assert decision.kind == "ALLOW"
    assert decision.reason_code is None
    assert decision.detail == {}


async def test_unknown_tool_is_denied_without_input_request(
    pipeline: PreToolUsePipeline,
) -> None:
    decision = await pipeline.evaluate(tool_call(stable_key="ghost.tool"))
    assert decision.kind == "DENY"
    assert decision.reason_code == UNKNOWN_TOOL
    assert decision.input_requirement is None


async def test_version_drift_is_denied(pipeline: PreToolUsePipeline) -> None:
    decision = await pipeline.evaluate(tool_call(version=2))
    assert decision.kind == "DENY"
    assert decision.reason_code == TOOL_VERSION_MISMATCH
    assert decision.input_requirement is None


# ---------------------------------------------------------------------------
# 模型输出质量:根结构 / 未允许字段 / 短路
# ---------------------------------------------------------------------------


async def test_non_object_root_is_model_error(pipeline: PreToolUsePipeline) -> None:
    decision = await pipeline.evaluate(tool_call(raw_arguments="[]"))
    assert decision.kind == "DENY"
    assert decision.reason_code == INVALID_MODEL_OUTPUT
    assert decision.input_requirement is None


async def test_extra_field_is_model_error_not_input_request(
    pipeline: PreToolUsePipeline,
) -> None:
    resolver = RecordingInputRequirementResolver()
    strict_pipeline = make_pipeline(input_requirement_resolver=resolver)
    decision = await strict_pipeline.evaluate(
        tool_call(raw_arguments=json.dumps({"query": "shoes", "price": "1.00"}))
    )
    assert decision.kind == "DENY"
    assert decision.reason_code == INVALID_MODEL_OUTPUT
    assert decision.input_requirement is None
    assert resolver.received is None


async def test_deny_short_circuits_remaining_stages(
    recording_pipeline: RecordingPipeline,
) -> None:
    decision = await recording_pipeline.evaluate(
        tool_call(raw_arguments=json.dumps({"query": "shoes", "price": "1.00"}))
    )
    assert decision.kind == "DENY"
    assert decision.reason_code == INVALID_MODEL_OUTPUT
    assert recording_pipeline.calls == ["tool_resolution", "schema_validation"]


# ---------------------------------------------------------------------------
# 缺失业务字段 → REQUIRE_INPUT
# ---------------------------------------------------------------------------


async def test_missing_business_fields_require_input_with_diagnostic() -> None:
    resolver = RecordingInputRequirementResolver()
    incomplete_pipeline = make_pipeline(input_requirement_resolver=resolver)
    decision = await incomplete_pipeline.evaluate(
        tool_call(
            stable_key="listing.publish",
            raw_arguments=json.dumps({"listing_id": "l-1"}),
        )
    )
    assert decision.kind == "REQUIRE_INPUT"
    assert decision.input_requirement is not None
    assert decision.schema_diagnostic is not None
    assert decision.schema_diagnostic.field_paths == ("store_id",)
    assert resolver.received is decision.schema_diagnostic
    assert decision.detail["field_paths"] == ("store_id",)


async def test_business_input_requirement_without_diagnostic_requires_input() -> None:
    """Schema 无诊断时,业务层仍可按规范化参数要求补充执行前提字段。

    ``location_ref`` 这类字段在 Schema 中可选:缺省不会产生任何 SchemaDiagnostic,
    但多位置店铺下它是执行前提。管线必须在无诊断时也咨询业务层,否则该需求
    永远不会变成 REQUIRE_INPUT。
    """
    resolver = BusinessInputRequirementResolver(
        InputRequirement(
            field_paths=("location_ref",),
            question="请选择要操作的位置",
            options={"location_ref": ({"value": "默认仓", "label": "默认仓"},)},
        )
    )
    business_pipeline = make_pipeline(input_requirement_resolver=resolver)

    decision = await business_pipeline.evaluate(valid_tool_call())

    assert decision.kind == "REQUIRE_INPUT"
    assert decision.schema_diagnostic is None
    assert decision.input_requirement is not None
    assert decision.input_requirement.field_paths == ("location_ref",)
    # 无诊断时 detail 的 field_paths 取自 InputRequirement 本身。
    assert decision.detail["field_paths"] == ("location_ref",)
    assert resolver.received_arguments == {"query": "running shoes"}


# ---------------------------------------------------------------------------
# 授权两阶段 / 业务策略
# ---------------------------------------------------------------------------


async def test_write_without_permission_is_denied() -> None:
    limited_pipeline = make_pipeline(
        actor=make_actor(permissions=frozenset({"listing:read"})),
        authorization_resource_resolver=ListingResourceResolver(),
    )
    decision = await limited_pipeline.evaluate(write_tool_call())
    assert decision.kind == "DENY"
    assert decision.reason_code == PERMISSION_DENIED


async def test_resource_out_of_scope_is_denied() -> None:
    scoped_pipeline = make_pipeline(
        actor=make_actor(listing_ids=("l-other",)),
        authorization_resource_resolver=ListingResourceResolver(),
    )
    decision = await scoped_pipeline.evaluate(write_tool_call())
    assert decision.kind == "DENY"
    assert decision.reason_code == RESOURCE_SCOPE_DENIED


async def test_business_policy_denial_becomes_deny() -> None:
    policy_pipeline = make_pipeline(
        authorization_resource_resolver=ListingResourceResolver(),
        business_policy=DenyingPolicy(),
    )
    decision = await policy_pipeline.evaluate(write_tool_call())
    assert decision.kind == "DENY"
    assert decision.reason_code == "LISTING_FROZEN"
    assert decision.detail["safe_message"] == "listing is frozen by policy"
    assert isinstance(decision.policy_decision, PolicyDecision)


# ---------------------------------------------------------------------------
# execution_mode 与 confirmation_verification
# ---------------------------------------------------------------------------


async def test_write_without_accepted_confirmation_requires_confirmation() -> None:
    confirming_pipeline = make_pipeline(
        authorization_resource_resolver=ListingResourceResolver(),
    )
    decision = await confirming_pipeline.evaluate(write_tool_call())
    assert decision.kind == "REQUIRE_CONFIRMATION"
    assert decision.reason_code == CONFIRMATION_REQUIRED
    assert isinstance(decision.confirmation_spec, ConfirmationSpec)


async def test_write_with_accepted_confirmation_allows() -> None:
    lookup = StaticConfirmationLookup(accepted=True)
    confirming_pipeline = make_pipeline(
        authorization_resource_resolver=ListingResourceResolver(),
        accepted_confirmation_lookup=lookup,
    )
    decision = await confirming_pipeline.evaluate(write_tool_call())
    assert decision.kind == "ALLOW"
    assert lookup.requested["stable_key"] == "listing.publish"
    assert lookup.requested["actor_id"] is not None


async def test_confirmation_spec_carries_current_policy_revision() -> None:
    """当前策略修订号进入 ConfirmationSpec,确认因此绑定"当时那一版规则"。"""
    confirming_pipeline = make_pipeline(
        authorization_resource_resolver=ListingResourceResolver(),
        business_policy=RevisionedPolicy(revision=7),
    )
    decision = await confirming_pipeline.evaluate(write_tool_call())
    assert decision.kind == "REQUIRE_CONFIRMATION"
    assert decision.confirmation_spec is not None
    assert decision.confirmation_spec.policy_version == 7


async def test_lookup_receives_current_policy_revision() -> None:
    """查询已接受确认时必须带上当前修订号,供指纹重算判定旧确认是否失效。"""
    lookup = StaticConfirmationLookup(accepted=True)
    confirming_pipeline = make_pipeline(
        authorization_resource_resolver=ListingResourceResolver(),
        business_policy=RevisionedPolicy(revision=9),
        accepted_confirmation_lookup=lookup,
    )
    decision = await confirming_pipeline.evaluate(write_tool_call())
    assert decision.kind == "ALLOW"
    assert lookup.requested["policy_version"] == 9


async def test_execution_mode_auto_skips_confirmation() -> None:
    lookup = StaticConfirmationLookup(accepted=False)
    auto_pipeline = make_pipeline(
        authorization_resource_resolver=ListingResourceResolver(),
        execution_mode_resolver=AutoModeResolver(),
        accepted_confirmation_lookup=lookup,
    )
    decision = await auto_pipeline.evaluate(write_tool_call())
    assert decision.kind == "ALLOW"
    assert lookup.requested == {}


# ---------------------------------------------------------------------------
# 决策对象与管线装配约束
# ---------------------------------------------------------------------------


async def test_decision_is_frozen_value_with_core_fields(
    pipeline: PreToolUsePipeline,
) -> None:
    decision = await pipeline.evaluate(valid_tool_call())
    assert isinstance(decision, PreToolDecision)
    assert decision.kind == "ALLOW"
    assert decision.reason_code is None
    assert decision.detail == {}
    with pytest.raises(ValidationError):
        decision.kind = "DENY"  # type: ignore[misc]


async def test_pipeline_rejects_reordered_or_incomplete_stages() -> None:
    stages = list(default_stages(build_deps(make_profile())))
    stages[0], stages[1] = stages[1], stages[0]
    with pytest.raises(Exception, match="重排"):
        PreToolUsePipeline(actor=make_actor(), stages=stages)
    with pytest.raises(Exception, match="重排"):
        PreToolUsePipeline(actor=make_actor(), stages=tuple(stages[:5]))
