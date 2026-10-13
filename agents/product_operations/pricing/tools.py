"""Pricing 能力的 ToolDefinition 与绑定(P1 Task 6;设计 §4.3、§8.1)。

发布两个价格 Tool:

- ``compare_listing_prices`` (v3):只读,输入换用模型可见引用 ``store_refs``;
- ``set_listing_price`` (v1):写入绝对目标价,输入为 ``store_ref``/``sku``/
  ``target_price``,内部标识与幂等凭据由服务端补全。

写 Tool 的 ``ToolBinding`` 需要请求级 Actor、Session 与平台 Gateway,不能由模块
级常量给出——因此只导出 Definition,由组合根用
:func:`build_set_listing_price_binding` 显式装配(与既有写 Tool 的约定一致)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import p1_fields_for, product_registration
from agents.product_operations.pricing.models import (
    CompareListingPricesInput,
    PriceComparisonSummary,
    SetListingPriceInput,
    SetListingPriceResult,
)
from agents.product_operations.pricing.service import (
    PRODUCT_PRICE_WRITE_PERMISSION,
    SET_LISTING_PRICE_VERSION,
    STABLE_KEY_SET_LISTING_PRICE,
    PricingService,
)
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.pretool.profiles import PreToolProfileRegistration
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_COMPARE_LISTING_PRICES: Final = "compare_listing_prices"
COMPARE_LISTING_PRICES_VERSION: Final = 3


def _strict_input_schema(model: type[Any], *, additional_properties: bool) -> dict[str, Any]:
    """派生 Input JSON Schema;写 Tool 关闭额外字段(未允许字段在 Schema 层拒绝)。"""
    schema: dict[str, Any] = model.model_json_schema()
    schema["additionalProperties"] = additional_properties
    return schema


COMPARE_LISTING_PRICES_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_COMPARE_LISTING_PRICES,
    version=COMPARE_LISTING_PRICES_VERSION,
    description=(
        "对比同一 SKU 在各授权店铺的已发布 Listing 售价,由确定性代码返回"
        "价格行、最低价、最高价与价差;店铺用名称/别名引用。多币种时拒绝"
        "直接比较。"
    ),
    input_json_schema=CompareListingPricesInput.model_json_schema(),
    output_json_schema=PriceComparisonSummary.model_json_schema(),
    effect=ToolEffect.READ,
    required_permissions=frozenset({"product.read"}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        CompareListingPricesInput.model_json_schema(),
        PriceComparisonSummary.model_json_schema(),
    ),
)


SET_LISTING_PRICE_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_SET_LISTING_PRICE,
    version=SET_LISTING_PRICE_VERSION,
    description=(
        "将授权店铺内指定 SKU 的 Listing 设为绝对目标价。输入为店铺引用、"
        "SKU 与目标价;币种、Listing 版本与幂等凭据由服务端补全;写前始终"
        "重跑价格策略,是否需要人工确认由 Workspace 的 ExecutionMode 决定。"
    ),
    input_json_schema=_strict_input_schema(
        SetListingPriceInput, additional_properties=False
    ),
    output_json_schema=SetListingPriceResult.model_json_schema(),
    effect=ToolEffect.WRITE,
    required_permissions=frozenset({PRODUCT_PRICE_WRITE_PERMISSION}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        _strict_input_schema(SetListingPriceInput, additional_properties=False),
        SetListingPriceResult.model_json_schema(),
    ),
)


async def compare_listing_prices(
    *,
    actor: ActorContext,
    session: AsyncSession,
    sku: str,
    store_refs: Sequence[str] = (),
) -> PriceComparisonSummary:
    """经 :class:`PricingService` 比较价格;授权校验与店铺解析都在 Service 内。"""
    return await PricingService(session).compare_listing_prices(
        actor=actor, sku=sku, store_refs=store_refs
    )


COMPARE_LISTING_PRICES_BINDING: Final = ToolBinding(
    definition=COMPARE_LISTING_PRICES_DEFINITION,
    handler=compare_listing_prices,
    pretool_registration=product_registration(
        fields=p1_fields_for(STABLE_KEY_COMPARE_LISTING_PRICES),
        policy_mode="NONE",
        resource_mode="WORKSPACE",
    ),
)


def build_set_listing_price_binding(
    *,
    handler: Any,
    pretool_registration: PreToolProfileRegistration,
    execution_profile: Any | None = None,
    write_adapter_capabilities: frozenset[str] = frozenset(),
) -> ToolBinding:
    """由组合根装配写 Tool 的绑定(Handler 与 Profile 均为请求/平台相关)。"""
    return ToolBinding(
        definition=SET_LISTING_PRICE_DEFINITION,
        handler=handler,
        pretool_registration=pretool_registration,
        execution_profile=execution_profile,
        write_adapter_capabilities=write_adapter_capabilities,
    )


__all__ = [
    "COMPARE_LISTING_PRICES_BINDING",
    "COMPARE_LISTING_PRICES_DEFINITION",
    "COMPARE_LISTING_PRICES_VERSION",
    "SET_LISTING_PRICE_DEFINITION",
    "SET_LISTING_PRICE_VERSION",
    "STABLE_KEY_COMPARE_LISTING_PRICES",
    "STABLE_KEY_SET_LISTING_PRICE",
    "build_set_listing_price_binding",
    "compare_listing_prices",
]
