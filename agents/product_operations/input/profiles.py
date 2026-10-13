"""商品各 Tool 的 PreToolProfile 工厂(计划 04 Task 3;架构文档 §3.3、§5、§9)。

每个商品 Tool 通过自己的 ToolBinding 声明一个 :class:`PreToolProfile`:

- 参数归一化只处理**本 Tool 的字段集合**(``TOOL_ARGUMENT_FIELDS``),只读
  Tool 的 ``query`` / ``store_ids`` 因此不再被写字段白名单吞掉;
- 授权资源解析按 Tool 选择单店铺 / 多店铺 / 无资源;
- 业务策略:只读 Tool 用显式 :class:`NoBusinessPolicy`,写 Tool 由组合根注入
  改价策略工厂(``business_policy_factory``)。

工厂本身在组合期创建一次并随 ToolBinding 静态绑定;它不持有启动期 Session,
而是在 ``create`` 时用请求级 :class:`PreToolProfileContext` 延迟构造协作对象。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.requirements import (
    ProductInputRequirementResolver,
    ProductPreToolInputRequirementResolver,
    ProductPreToolNormalizer,
    ProductPreToolProvenanceResolver,
)
from runtime.execution.pretool.profiles import (
    NoAuthorizationResources,
    NoBusinessPolicy,
    PreToolProfile,
    PreToolProfileContext,
    PreToolProfileRegistration,
)
from runtime.persistence.base import MessageRole
from runtime.persistence.models.interaction import Message
from runtime.persistence.repositories.runs import RunRepository


#: 每个商品 Tool 的字段集合(参数归一化白名单与关键字段判定范围)。
#: 与各 ToolDefinition.stable_key 一致,由 test_tool_profiles 断言防漂移。
TOOL_ARGUMENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "search_products": ("query", "store_ids"),
    "get_product_snapshot": ("store_id", "sku"),
    "compare_listing_prices": ("sku", "store_ids"),
    "update_price": ("store_id", "sku", "target_price"),
}

#: P1 新版本 Tool 的模型可见字段集合(设计 §4.1:用 ``store_ref``/``store_refs``
#: 引用店铺,由 Service 经授权的 StoreReferenceResolver 解析成内部 UUID)。
#: 与旧版本同 stable key 者按版本区分:``search_products`` 的 v3 用
#: ``store_refs``,旧 v2 仍用 ``store_ids``,因此不并入上面的映射。
P1_TOOL_ARGUMENT_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "search_products": ("query", "store_refs"),
    "get_listing": ("store_ref", "sku"),
    "compare_listing_prices": ("sku", "store_refs"),
    "set_listing_price": ("store_ref", "sku", "target_price"),
    "get_inventory_level": ("store_ref", "sku", "location_ref"),
    "adjust_inventory_quantity": ("store_ref", "sku", "location_ref", "delta", "reason"),
    "publish_listing": ("store_ref", "sku"),
    "unpublish_listing": ("store_ref", "sku", "reason"),
}

BusinessPolicyFactory = Callable[[PreToolProfileContext], Any]

#: 需要"来源守卫"的关键字段:已提供但来源不可信时必须退回 Input Request。
#: 取值来自架构文档 §5:店铺与 SKU 是可被模型猜测的写目标;金额由确定性
#: 归一化判定,不属守卫范围。
PROVENANCE_GUARD_FIELDS: Final = ("store_id", "sku")


async def current_user_message(
    session: AsyncSession, workspace_id: UUID, run_id: UUID | None
) -> str:
    """当前用户消息:该 Run 会话内最后一条 USER 消息(Provenance 证据来源)。

    ``run_id`` 为空(如未绑定 Run 的单测)时返回空串,不猜测证据。
    """
    if run_id is None:
        return ""
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


class ProductPreToolProfileFactory:
    """按请求级上下文创建商品 Tool 的 PreToolProfile。"""

    def __init__(
        self,
        *,
        fields: Sequence[str],
        business_policy_factory: BusinessPolicyFactory | None = None,
        resource_resolver: Any | None = None,
    ) -> None:
        self._fields = tuple(fields)
        self._business_policy_factory = business_policy_factory
        self._resource_resolver = resource_resolver or NoAuthorizationResources()

    async def create(self, context: PreToolProfileContext) -> PreToolProfile:
        resolver = ProductInputRequirementResolver(context.session)
        user_message = await current_user_message(
            context.session, context.actor.workspace_id, context.run_id
        )
        business_policy = (
            self._business_policy_factory(context)
            if self._business_policy_factory is not None
            else NoBusinessPolicy()
        )
        return PreToolProfile(
            input_normalizer=ProductPreToolNormalizer(
                resolver,
                fields=self._fields,
                user_message=user_message,
                actor=context.actor,
                answered=context.answered,
            ),
            argument_provenance_resolver=ProductPreToolProvenanceResolver(
                resolver,
                user_message=user_message,
                actor=context.actor,
                answered=context.answered,
                fields=self._fields,
            ),
            input_requirement_resolver=ProductPreToolInputRequirementResolver(
                resolver, fields=self._fields, actor=context.actor
            ),
            authorization_resource_resolver=self._resource_resolver,
            business_policy=business_policy,
            provenance_guard_fields=tuple(
                field for field in self._fields if field in PROVENANCE_GUARD_FIELDS
            ),
        )


def product_registration(
    *,
    fields: Sequence[str],
    policy_mode: str = "NONE",
    resource_mode: str = "WORKSPACE",
    business_policy_factory: BusinessPolicyFactory | None = None,
    resource_resolver: Any | None = None,
) -> PreToolProfileRegistration:
    """构造商品 Tool 的启动期注册描述(供组合根与 ToolBinding 常量复用)。"""
    return PreToolProfileRegistration(
        factory=ProductPreToolProfileFactory(
            fields=fields,
            business_policy_factory=business_policy_factory,
            resource_resolver=resource_resolver,
        ),
        policy_mode=policy_mode,  # type: ignore[arg-type]
        resource_mode=resource_mode,  # type: ignore[arg-type]
    )


def fields_for(stable_key: str) -> tuple[str, ...]:
    """返回该 Tool 的字段集合;未知 Tool 抛错,不静默回退。"""
    try:
        return TOOL_ARGUMENT_FIELDS[stable_key]
    except KeyError as error:  # pragma: no cover - 防御
        raise KeyError(f"no product argument fields declared for {stable_key}") from error


def p1_fields_for(stable_key: str) -> tuple[str, ...]:
    """返回 P1 新版本 Tool 的字段集合;未知 Tool 抛错,不静默回退。"""
    try:
        return P1_TOOL_ARGUMENT_FIELDS[stable_key]
    except KeyError as error:  # pragma: no cover - 防御
        raise KeyError(
            f"no P1 product argument fields declared for {stable_key}"
        ) from error


__all__ = [
    "P1_TOOL_ARGUMENT_FIELDS",
    "TOOL_ARGUMENT_FIELDS",
    "BusinessPolicyFactory",
    "ProductPreToolProfileFactory",
    "current_user_message",
    "fields_for",
    "p1_fields_for",
    "product_registration",
]
