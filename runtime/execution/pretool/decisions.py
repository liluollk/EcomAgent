"""PreToolUse 管线的统一决策与阶段值对象(计划 02 Task 6;架构文档 §9)。

四个决策种类(ALLOW/DENY/REQUIRE_INPUT/REQUIRE_CONFIRMATION)与阶段间交换的
数据对象全部是不可变值对象;业务扩展只返回数据,不直接修改 Run、ToolCall
或 Request 状态。``InputRequirement``/``ConfirmationSpec`` 的字段结构由交互
阶段(计划 02 Task 7)定义,此处只复用 ``runtime.interfaces.requests`` 既有
骨架类型。
"""

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from runtime.interfaces.requests import ConfirmationSpec, InputRequirement


# ---- 固定阶段名(架构文档 §9;顺序不可重排)----

STAGE_TOOL_RESOLUTION: Final = "tool_resolution"
STAGE_SCHEMA_VALIDATION: Final = "schema_validation"
STAGE_INPUT_NORMALIZATION: Final = "input_normalization"
STAGE_ARGUMENT_PROVENANCE: Final = "argument_provenance"
STAGE_INPUT_REQUIREMENT: Final = "input_requirement"
STAGE_AUTHORIZATION_RESOURCE: Final = "authorization_resource"
STAGE_ACCESS_CONTROL: Final = "access_control"
STAGE_BUSINESS_POLICY: Final = "business_policy"
STAGE_EXECUTION_MODE: Final = "execution_mode"
STAGE_CONFIRMATION_VERIFICATION: Final = "confirmation_verification"

PRETOOL_STAGE_ORDER: Final = (
    STAGE_TOOL_RESOLUTION,
    STAGE_SCHEMA_VALIDATION,
    STAGE_INPUT_NORMALIZATION,
    STAGE_ARGUMENT_PROVENANCE,
    STAGE_INPUT_REQUIREMENT,
    STAGE_AUTHORIZATION_RESOURCE,
    STAGE_ACCESS_CONTROL,
    STAGE_BUSINESS_POLICY,
    STAGE_EXECUTION_MODE,
    STAGE_CONFIRMATION_VERIFICATION,
)

# ---- 原因码 ----

INVALID_MODEL_OUTPUT: Final = "INVALID_MODEL_OUTPUT"
UNKNOWN_TOOL: Final = "UNKNOWN_TOOL"
TOOL_VERSION_MISMATCH: Final = "TOOL_VERSION_MISMATCH"
TOOL_DISABLED: Final = "TOOL_DISABLED"
TOOL_PRETOOL_PROFILE_MISSING: Final = "TOOL_PRETOOL_PROFILE_MISSING"
ARGUMENTS_INCOMPLETE: Final = "ARGUMENTS_INCOMPLETE"
POLICY_DENIED: Final = "POLICY_DENIED"
CONFIRMATION_REQUIRED: Final = "CONFIRMATION_REQUIRED"
EXECUTION_MODE_READ_ONLY: Final = "EXECUTION_MODE_READ_ONLY"
ACCESS_DENIED: Final = "ACCESS_DENIED"
PIPELINE_INVARIANT: Final = "PIPELINE_INVARIANT"

# ---- 阶段值对象 ----


class SchemaDiagnostic(BaseModel):
    """Schema Validator 对缺失/不完整业务字段的结构化诊断。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_paths: tuple[str, ...]
    message: str


class PolicyDecision(BaseModel):
    """Business Policy 阶段的结构化返回值;只是值对象,不持有 Repository。

    ``resource_versions`` 允许业务策略把授权资源的版本(如
    ``{"<resource_type>:<id>": 3}``)交给 Runtime 写进 ConfirmationSpec,从而在
    确认请求上持久化、跨进程重启依然可比对(架构文档 §6 第 7 条)。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: str | None = None
    safe_message: str | None = None
    resource_versions: Mapping[str, int] = {}


class ArgumentProvenance(BaseModel):
    """参数来源判定结果;只来自确定性规则,不接受模型自报来源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_sources: Mapping[str, str] = {}


class ExecutionModeDecision(BaseModel):
    """Execution Mode 阶段决策:只读、确认或自动执行。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["READ_ONLY", "CONFIRM", "AUTO", "DENY"]


# ---- 统一决策 ----

PreToolDecisionKind = Literal["ALLOW", "DENY", "REQUIRE_INPUT", "REQUIRE_CONFIRMATION"]


class PreToolDecision(BaseModel):
    """PreToolUse 统一决策(架构文档 §9)。

    - ``kind``:四种决策之一;DENY 携带 ``reason_code``,
      REQUIRE_INPUT 携带 ``input_requirement`` + ``schema_diagnostic``,
      REQUIRE_CONFIRMATION 携带 ``confirmation_spec``;
    - ``detail``:面向展示的补充信息(如 ``safe_message``、``field_paths``),
      与内部诊断分离。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PreToolDecisionKind
    reason_code: str | None = None
    detail: Mapping[str, Any] = {}
    input_requirement: InputRequirement | None = None
    schema_diagnostic: SchemaDiagnostic | None = None
    confirmation_spec: ConfirmationSpec | None = None
    policy_decision: PolicyDecision | None = None
    normalized_arguments: Mapping[str, Any] | None = None


def allow() -> PreToolDecision:
    """全部阶段通过。"""
    return PreToolDecision(kind="ALLOW")


def deny(
    reason_code: str,
    *,
    safe_message: str | None = None,
    policy_decision: PolicyDecision | None = None,
) -> PreToolDecision:
    """拒绝决策;``safe_message`` 进入 detail,与内部诊断分离。"""
    detail: dict[str, Any] = {}
    if safe_message is not None:
        detail["safe_message"] = safe_message
    return PreToolDecision(
        kind="DENY",
        reason_code=reason_code,
        detail=detail,
        policy_decision=policy_decision,
    )


def require_input(
    *, input_requirement: InputRequirement, schema_diagnostic: SchemaDiagnostic
) -> PreToolDecision:
    """需要用户补充输入;决策同时携带 InputRequirement 与 SchemaDiagnostic。"""
    return PreToolDecision(
        kind="REQUIRE_INPUT",
        detail={"field_paths": schema_diagnostic.field_paths},
        input_requirement=input_requirement,
        schema_diagnostic=schema_diagnostic,
    )


def require_confirmation(*, confirmation_spec: ConfirmationSpec) -> PreToolDecision:
    """需要已接受的 Confirmation 才能执行。"""
    return PreToolDecision(
        kind="REQUIRE_CONFIRMATION",
        reason_code=CONFIRMATION_REQUIRED,
        confirmation_spec=confirmation_spec,
    )


__all__ = [
    "ACCESS_DENIED",
    "ARGUMENTS_INCOMPLETE",
    "CONFIRMATION_REQUIRED",
    "EXECUTION_MODE_READ_ONLY",
    "INVALID_MODEL_OUTPUT",
    "PIPELINE_INVARIANT",
    "POLICY_DENIED",
    "PRETOOL_STAGE_ORDER",
    "STAGE_ACCESS_CONTROL",
    "STAGE_ARGUMENT_PROVENANCE",
    "STAGE_AUTHORIZATION_RESOURCE",
    "STAGE_BUSINESS_POLICY",
    "STAGE_CONFIRMATION_VERIFICATION",
    "STAGE_EXECUTION_MODE",
    "STAGE_INPUT_NORMALIZATION",
    "STAGE_INPUT_REQUIREMENT",
    "STAGE_SCHEMA_VALIDATION",
    "STAGE_TOOL_RESOLUTION",
    "TOOL_DISABLED",
    "TOOL_PRETOOL_PROFILE_MISSING",
    "TOOL_VERSION_MISMATCH",
    "UNKNOWN_TOOL",
    "ArgumentProvenance",
    "ExecutionModeDecision",
    "PolicyDecision",
    "PreToolDecision",
    "PreToolDecisionKind",
    "SchemaDiagnostic",
    "allow",
    "deny",
    "require_confirmation",
    "require_input",
]
