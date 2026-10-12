"""Runtime 参数可信等级的唯一类型来源与 fail-closed 来源守卫(计划 04 Task 8)。

覆盖:

- ``ArgumentProvenance.field_sources`` 只接受 :class:`ArgumentTrustLevel`,
  未知/拼写错误的等级字符串被拒绝(不能绕过来源守卫);
- 可信等级白名单不含 ``UNTRUSTED``;
- Tool Profile 声明的守卫关键字段"已提供但来源不可信或缺失"时,
  argument_provenance 阶段统一并入 SchemaDiagnostic,交 input_requirement
  转成可回答的 Input Request;
- 来源可信时不触发守卫。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from runtime.access.actor import ActorContext
from runtime.access.authorization import Authorizer
from runtime.capabilities.registry import ToolBinding, ToolRegistry, ToolVersionRecord
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.pretool.decisions import ArgumentProvenance
from runtime.execution.pretool.pipeline import (
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.profiles import (
    PassThroughInputNormalizer,
    PreToolProfile,
    PreToolProfileContext,
    PreToolProfileRegistration,
    no_op_profile,
)
from runtime.execution.pretool.provenance import (
    TRUSTED_TRUST_LEVELS,
    ArgumentTrustLevel,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.interfaces.identifiers import ToolCallId
from runtime.interfaces.requests import InputRequirement
from runtime.interfaces.tools import ToolDefinition, ToolEffect
from runtime.persistence.base import MembershipRole


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"store_id": {"type": "string"}},
    "required": ["store_id"],
    "additionalProperties": False,
}

GUARDED_TOOL_KEY = "listing.publish"


# ---- 等级类型唯一性 ----


def test_unknown_trust_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ArgumentProvenance(field_sources={"store_id": "TRUSTED_BY_MODEL"})


def test_known_trust_levels_are_accepted() -> None:
    provenance = ArgumentProvenance(
        field_sources={
            "store_id": ArgumentTrustLevel.USER_EXPLICIT,
            "sku": "SYSTEM_RESOLVED",
        }
    )
    assert provenance.field_sources["store_id"] is ArgumentTrustLevel.USER_EXPLICIT
    # str 枚举:JSON/字典字面量仍按值匹配。
    assert provenance.field_sources["sku"] == ArgumentTrustLevel.SYSTEM_RESOLVED


def test_untrusted_is_not_a_trusted_level() -> None:
    assert ArgumentTrustLevel.UNTRUSTED not in TRUSTED_TRUST_LEVELS
    assert TRUSTED_TRUST_LEVELS == frozenset(
        {
            ArgumentTrustLevel.USER_EXPLICIT,
            ArgumentTrustLevel.USER_CONFIRMED,
            ArgumentTrustLevel.SYSTEM_RESOLVED,
            ArgumentTrustLevel.DETERMINISTIC_DERIVED,
        }
    )


# ---- 来源守卫:缺失来源按不可信处理 ----


def _definition() -> ToolDefinition:
    return ToolDefinition(
        stable_key=GUARDED_TOOL_KEY,
        version=1,
        description=GUARDED_TOOL_KEY,
        input_json_schema=INPUT_SCHEMA,
        output_json_schema={"type": "object"},
        effect=ToolEffect.READ,
        required_permissions=frozenset(),
        source_kind="LOCAL",
        schema_hash=compute_schema_hash(INPUT_SCHEMA, {"type": "object"}),
    )


class _StubProvenanceResolver:
    """返回固定 Provenance 的业务替身。"""

    def __init__(self, provenance: ArgumentProvenance) -> None:
        self._provenance = provenance

    async def resolve(self, *, arguments: Any) -> ArgumentProvenance:
        del arguments
        return self._provenance


class _RecordingRequirementResolver:
    """把诊断转成 InputRequirement,并记录收到的诊断。"""

    def __init__(self) -> None:
        self.received: Any = None

    async def resolve(self, *, diagnostic: Any) -> InputRequirement | None:
        self.received = diagnostic
        return InputRequirement(
            field_paths=diagnostic.field_paths, question="补充关键参数"
        )


class _StaticProfileFactory:
    def __init__(self, profile: PreToolProfile) -> None:
        self._profile = profile

    async def create(self, context: Any) -> PreToolProfile:
        del context
        return self._profile


def _profile_with(
    *,
    provenance: ArgumentProvenance,
    requirement_resolver: _RecordingRequirementResolver,
) -> PreToolProfile:
    base = no_op_profile()
    return PreToolProfile(
        input_normalizer=PassThroughInputNormalizer(),
        argument_provenance_resolver=_StubProvenanceResolver(provenance),
        input_requirement_resolver=requirement_resolver,
        authorization_resource_resolver=base.authorization_resource_resolver,
        business_policy=base.business_policy,
        provenance_guard_fields=("store_id",),
    )


def _pipeline(profile: PreToolProfile) -> PreToolUsePipeline:
    definition = _definition()
    registry = ToolRegistry()
    registry.register(
        ToolVersionRecord(
            stable_key=definition.stable_key,
            version_id=uuid4(),
            version=definition.version,
            source_kind="LOCAL",
            effect=definition.effect,
            input_schema=definition.input_json_schema,
            output_schema=definition.output_json_schema,
            schema_hash=definition.schema_hash,
            required_permissions=definition.required_permissions,
        )
    )
    registry.bind(
        ToolBinding(
            definition=definition,
            handler=object(),
            pretool_registration=PreToolProfileRegistration(
                factory=_StaticProfileFactory(profile),
                policy_mode="NONE",
                resource_mode="WORKSPACE",
            ),
        )
    )
    actor = ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        membership_id=uuid4(),
        role=MembershipRole.WORKSPACE_OWNER,
        permissions=frozenset(),
        resource_scopes={},
        request_id="req-provenance",
    )
    dependencies = PreToolDependencies(
        tool_registry=RuntimeToolRegistry(registry),
        schema_validator=RuntimeSchemaValidator(),
        authorizer=Authorizer(),
        profile_context=PreToolProfileContext(
            session=None, actor=actor, request=_request()
        ),
    )
    return PreToolUsePipeline(actor=actor, stages=default_stages(dependencies))


def _request() -> ToolCallRequest:
    return ToolCallRequest(
        tool_call_id=ToolCallId(uuid4()),
        stable_key=GUARDED_TOOL_KEY,
        version=1,
        raw_arguments=json.dumps({"store_id": "s-1"}),
    )


async def test_missing_source_for_provided_guard_field_requires_input() -> None:
    """字段已提供但未声明来源 → 按 UNTRUSTED 处理,回到 Input Request。"""
    resolver = _RecordingRequirementResolver()
    profile = _profile_with(
        provenance=ArgumentProvenance(),  # 不声明任何来源
        requirement_resolver=resolver,
    )

    decision = await _pipeline(profile).evaluate(_request())

    assert decision.kind == "REQUIRE_INPUT"
    assert resolver.received is not None
    assert resolver.received.field_paths == ("store_id",)


async def test_untrusted_source_for_guard_field_requires_input() -> None:
    resolver = _RecordingRequirementResolver()
    profile = _profile_with(
        provenance=ArgumentProvenance(
            field_sources={"store_id": ArgumentTrustLevel.UNTRUSTED}
        ),
        requirement_resolver=resolver,
    )

    decision = await _pipeline(profile).evaluate(_request())

    assert decision.kind == "REQUIRE_INPUT"
    assert resolver.received is not None
    assert resolver.received.field_paths == ("store_id",)


async def test_trusted_source_for_guard_field_passes() -> None:
    resolver = _RecordingRequirementResolver()
    profile = _profile_with(
        provenance=ArgumentProvenance(
            field_sources={"store_id": ArgumentTrustLevel.USER_EXPLICIT}
        ),
        requirement_resolver=resolver,
    )

    decision = await _pipeline(profile).evaluate(_request())

    assert decision.kind == "ALLOW"
    assert resolver.received is None
