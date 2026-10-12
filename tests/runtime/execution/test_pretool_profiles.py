"""PreToolProfile 与启动期 fail-closed 校验测试(计划 04 Task 2;架构文档 §3.3、§9)。

覆盖:

- 只读 Tool 显式绑定 ``NoBusinessPolicy`` 可以注册;
- 写 Tool 缺少 ``business_policy`` / ``resource_resolver`` / ``execution_profile``
  分别以稳定错误码拒绝;
- 只读 Tool 绑定执行 Profile 被拒绝;
- 未注册 Profile 的 ToolBinding 不允许依赖 Pipeline 的隐式 ``None`` 默认值,
  在 ``tool_resolution`` 后确定性拒绝。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from runtime.access.actor import ActorContext
from runtime.access.authorization import Authorizer
from runtime.capabilities.registry import ToolBinding, ToolRegistry, ToolVersionRecord
from runtime.capabilities.validation import (
    READ_TOOL_EXECUTION_PROFILE_FORBIDDEN,
    WRITE_TOOL_EXECUTION_PROFILE_MISSING,
    WRITE_TOOL_POLICY_MISSING,
    WRITE_TOOL_RESOURCE_RESOLVER_MISSING,
    AssemblyError,
    compute_schema_hash,
    validate_tool_binding,
)
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.pretool.decisions import TOOL_PRETOOL_PROFILE_MISSING
from runtime.execution.pretool.pipeline import (
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.profiles import (
    PreToolProfileRegistration,
    no_op_profile,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.interfaces.identifiers import ToolCallId
from runtime.interfaces.tools import ToolDefinition, ToolEffect
from runtime.persistence.base import MembershipRole


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"store_id": {"type": "string"}},
    "required": ["store_id"],
    "additionalProperties": False,
}


def _definition(key: str, effect: ToolEffect) -> ToolDefinition:
    return ToolDefinition(
        stable_key=key,
        version=1,
        description=key,
        input_json_schema=INPUT_SCHEMA,
        output_json_schema={"type": "object"},
        effect=effect,
        required_permissions=frozenset(),
        source_kind="LOCAL",
        schema_hash=compute_schema_hash(INPUT_SCHEMA, {"type": "object"}),
    )


class _StaticFactory:
    """测试替身工厂:无视请求级上下文,固定返回空 Profile。"""

    async def create(self, context: Any) -> Any:
        del context
        return no_op_profile()


def _registration(
    *, policy_mode: str = "NONE", resource_mode: str = "WORKSPACE"
) -> PreToolProfileRegistration:
    return PreToolProfileRegistration(
        factory=_StaticFactory(),
        policy_mode=policy_mode,  # type: ignore[arg-type]
        resource_mode=resource_mode,  # type: ignore[arg-type]
    )


def _binding(
    key: str,
    effect: ToolEffect,
    *,
    policy_mode: str = "NONE",
    resource_mode: str = "WORKSPACE",
    execution_profile: ToolExecutionProfile | None = None,
) -> ToolBinding:
    return ToolBinding(
        definition=_definition(key, effect),
        handler=object(),
        pretool_registration=_registration(
            policy_mode=policy_mode, resource_mode=resource_mode
        ),
        execution_profile=execution_profile,
    )


READ_PROFILE = ToolExecutionProfile(adapter_key="pricing-mock")


# ---- 启动期 Profile 校验 ----


def test_read_binding_with_explicit_no_business_policy_is_valid() -> None:
    validate_tool_binding(_binding("listing.search", ToolEffect.READ))


def test_write_binding_requires_business_policy() -> None:
    with pytest.raises(AssemblyError, match="business_policy"):
        validate_tool_binding(
            _binding(
                "listing.publish",
                ToolEffect.WRITE,
                policy_mode="NONE",
                resource_mode="RESOURCE",
                execution_profile=READ_PROFILE,
            )
        )
    assert WRITE_TOOL_POLICY_MISSING == "WRITE_TOOL_POLICY_MISSING"


def test_write_binding_requires_resource_resolver() -> None:
    with pytest.raises(AssemblyError, match=WRITE_TOOL_RESOURCE_RESOLVER_MISSING):
        validate_tool_binding(
            _binding(
                "listing.publish",
                ToolEffect.WRITE,
                policy_mode="BUSINESS",
                resource_mode="WORKSPACE",
                execution_profile=READ_PROFILE,
            )
        )


def test_write_binding_requires_execution_profile() -> None:
    with pytest.raises(AssemblyError, match=WRITE_TOOL_EXECUTION_PROFILE_MISSING):
        validate_tool_binding(
            _binding(
                "listing.publish",
                ToolEffect.WRITE,
                policy_mode="BUSINESS",
                resource_mode="RESOURCE",
                execution_profile=None,
            )
        )


def test_read_binding_forbids_execution_profile() -> None:
    with pytest.raises(AssemblyError, match=READ_TOOL_EXECUTION_PROFILE_FORBIDDEN):
        validate_tool_binding(
            _binding(
                "listing.search",
                ToolEffect.READ,
                execution_profile=READ_PROFILE,
            )
        )


def test_fully_declared_write_binding_is_valid() -> None:
    validate_tool_binding(
        _binding(
            "listing.publish",
            ToolEffect.WRITE,
            policy_mode="BUSINESS",
            resource_mode="RESOURCE",
            execution_profile=READ_PROFILE,
        )
    )


# ---- 未注册 Profile:Pipeline fail-closed ----


def _actor() -> ActorContext:
    return ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        membership_id=uuid4(),
        role=MembershipRole.WORKSPACE_OWNER,
        permissions=frozenset(),
        resource_scopes={},
        request_id="req-profile",
    )


def _registry_with_unregistered_binding() -> ToolRegistry:
    registry = ToolRegistry()
    definition = _definition("listing.search", ToolEffect.READ)
    record = ToolVersionRecord(
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
    registry.register(record, make_current=True)
    # 模拟历史/异常绑定:绕过必填字段校验,不注册任何 PreToolProfile。
    registry.bind(
        record.version_id,
        ToolBinding.model_construct(
            definition=definition,
            handler=object(),
            pretool_registration=None,
            execution_profile=None,
            write_adapter_capabilities=frozenset(),
        )
    )
    return registry


async def test_pipeline_denies_binding_without_registered_profile() -> None:
    dependencies = PreToolDependencies(
        tool_registry=RuntimeToolRegistry(_registry_with_unregistered_binding()),
        schema_validator=RuntimeSchemaValidator(),
        authorizer=Authorizer(),
    )
    decision = await PreToolUsePipeline(
        actor=_actor(), stages=default_stages(dependencies)
    ).evaluate(
        ToolCallRequest(
            tool_call_id=ToolCallId(uuid4()),
            stable_key="listing.search",
            version=1,
            raw_arguments=json.dumps({"store_id": "s-1"}),
        )
    )
    assert decision.kind == "DENY"
    assert decision.reason_code == TOOL_PRETOOL_PROFILE_MISSING
