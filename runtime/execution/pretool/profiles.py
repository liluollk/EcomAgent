"""PreToolProfile:每个 ToolBinding 自己的 PreToolUse 业务扩展(计划 04 Task 2)。

固定 10 阶段顺序仍由 Runtime 管理(架构文档 §9);每个 Tool 通过 ToolBinding
提供自己的 :class:`PreToolProfile`,把"参数归一化、来源判定、缺失字段解析、
授权资源解析、业务策略"绑定到**单个 Tool**,而不是由组合根按 ``stable_key``
全局注入。

分两层职责:

- 启动期:只读 :class:`PreToolProfileRegistration` 的 ``policy_mode`` /
  ``resource_mode`` 做 fail-closed 校验(见
  :func:`runtime.capabilities.validation.validate_tool_binding`);
- 运行期:由 :class:`PreToolProfileFactory` 用请求级上下文(当前 Session、
  Actor、ToolCall 与 Run 范围)延迟创建 Profile,因此 ToolBinding 中绝不保存
  长生命周期数据库 Session。

``NoBusinessPolicy`` 只能被显式创建:它是只读 Tool 的显式空策略,不得成为
任何写 Tool 的默认策略(写 Tool 缺策略在启动期即拒绝)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from runtime.execution.pretool.decisions import (
    ArgumentProvenance,
    PolicyDecision,
)
from runtime.interfaces.requests import InputRequirement


if TYPE_CHECKING:  # pragma: no cover - 仅类型检查需要
    from sqlalchemy.ext.asyncio import AsyncSession

    from runtime.access.actor import ActorContext
    from runtime.execution.pretool.pipeline import (
        ArgumentProvenanceResolver,
        AuthorizationResourceResolver,
        BusinessPolicy,
        InputNormalizer,
        InputRequirementResolver,
        ToolCallRequest,
    )


PolicyMode = Literal["NONE", "BUSINESS"]
ResourceMode = Literal["WORKSPACE", "RESOURCE"]


# ---- 显式空实现:只读 Tool 与 Runtime 内置 Tool 使用 ----


class NoBusinessPolicy:
    """显式空业务策略:放行一切;只允许被显式注册,不作为缺省值。"""

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        del stable_key, arguments, resources
        return PolicyDecision(allowed=True)


class PassThroughInputNormalizer:
    """不做任何归一化:Runtime 内置 Tool 直接沿用 Schema 校验后的参数。"""

    async def normalize(self, *, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return arguments


class EmptyProvenanceResolver:
    """空来源判定:不产生任何字段来源;Runtime 内置 Tool 走该实现。"""

    async def resolve(self, *, arguments: Mapping[str, Any]) -> ArgumentProvenance:
        del arguments
        return ArgumentProvenance()


class UnresolvableInputRequirement:
    """缺失字段不可补充:Runtime 内置 Tool 依赖 Schema 必填,不生成 Input。"""

    async def resolve(self, *, diagnostic: Any) -> InputRequirement | None:
        del diagnostic
        return None


class NoAuthorizationResources:
    """不解析授权资源:仅依赖 Tool 动作权限的 Tool 使用。"""

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> frozenset[tuple[str, str]]:
        del arguments
        return frozenset()


@dataclass(frozen=True)
class PreToolProfile:
    """单个 Tool 的 PreToolUse 业务扩展组合;扩展只返回值对象。

    五个协作对象显式给出(可以为上面的显式空实现),Pipeline 不再依赖
    ``None`` 缺省值做隐式回退。``provenance_guard_fields`` 声明"已提供但来源
    不可信时必须退回 Input Request"的关键字段(架构文档 §5):Runtime 在
    argument_provenance 阶段按此声明做统一守卫,业务不需要包装阶段。
    """

    input_normalizer: InputNormalizer
    argument_provenance_resolver: ArgumentProvenanceResolver
    input_requirement_resolver: InputRequirementResolver
    authorization_resource_resolver: AuthorizationResourceResolver
    business_policy: BusinessPolicy
    provenance_guard_fields: tuple[str, ...] = ()


def no_op_profile() -> PreToolProfile:
    """Runtime 内置 Tool(如 ``load_skill``)的显式空 Profile。"""
    return PreToolProfile(
        input_normalizer=PassThroughInputNormalizer(),
        argument_provenance_resolver=EmptyProvenanceResolver(),
        input_requirement_resolver=UnresolvableInputRequirement(),
        authorization_resource_resolver=NoAuthorizationResources(),
        business_policy=NoBusinessPolicy(),
        provenance_guard_fields=(),
    )


@dataclass(frozen=True)
class PreToolProfileContext:
    """创建 Profile 所需的请求级上下文;生命周期不超出单次 PreToolUse。

    ``run_id`` / ``answered`` 供业务 Profile 读取当前 Run 的用户证据(当前用户
    消息、已处理 Input Request 的结构化回答);Runtime 内置 Profile 不使用它们。
    """

    session: AsyncSession
    actor: ActorContext
    request: ToolCallRequest
    run_id: UUID | None = None
    answered: Mapping[str, str] = field(default_factory=dict)


class PreToolProfileFactory(Protocol):
    """按请求级上下文延迟创建 Profile;不持有启动期 Session。"""

    async def create(self, context: PreToolProfileContext) -> PreToolProfile:
        raise NotImplementedError


class PreToolProfileRegistration:
    """启动期 Profile 注册描述:只声明策略/资源模式,工厂延迟创建。

    刻意**不是** dataclass / pydantic 模型:``factory`` 字段是 Protocol,而
    ToolBinding 是 frozen pydantic 模型;若把本类型也交给 pydantic 递归建模会触发
    ``PydanticSchemaGenerationError``。这里用普通类承载,由 ToolBinding 以
    ``arbitrary_types_allowed`` 承接,并显式实现 ``__eq__`` / ``__repr__``。
    """

    __slots__ = ("factory", "policy_mode", "resource_mode")

    def __init__(
        self,
        *,
        factory: PreToolProfileFactory,
        policy_mode: PolicyMode,
        resource_mode: ResourceMode,
    ) -> None:
        self.factory = factory
        self.policy_mode = policy_mode
        self.resource_mode = resource_mode

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is PreToolProfileRegistration
            and self.factory is other.factory
            and self.policy_mode == other.policy_mode
            and self.resource_mode == other.resource_mode
        )

    def __hash__(self) -> int:
        return hash((id(self.factory), self.policy_mode, self.resource_mode))

    def __repr__(self) -> str:
        return (
            "PreToolProfileRegistration("
            f"factory={type(self.factory).__name__}, "
            f"policy_mode={self.policy_mode!r}, "
            f"resource_mode={self.resource_mode!r})"
        )


class NoOpProfileFactory:
    """Runtime 内置 Tool 的 Profile 工厂:始终返回显式空 Profile。"""

    async def create(self, context: PreToolProfileContext) -> PreToolProfile:
        del context
        return no_op_profile()


def runtime_control_registration() -> PreToolProfileRegistration:
    """Runtime 控制类 Tool(如 ``load_skill``)的显式空 Profile 注册。"""
    return PreToolProfileRegistration(
        factory=NoOpProfileFactory(),
        policy_mode="NONE",
        resource_mode="WORKSPACE",
    )


__all__ = [
    "EmptyProvenanceResolver",
    "NoAuthorizationResources",
    "NoBusinessPolicy",
    "NoOpProfileFactory",
    "PassThroughInputNormalizer",
    "PolicyMode",
    "PreToolProfile",
    "PreToolProfileContext",
    "PreToolProfileFactory",
    "PreToolProfileRegistration",
    "ResourceMode",
    "UnresolvableInputRequirement",
    "no_op_profile",
    "runtime_control_registration",
]
