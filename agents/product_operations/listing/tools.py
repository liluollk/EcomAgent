"""Listing 能力的 ToolDefinition 与绑定(P1 Task 5 查询;Task 8 发布状态写入)。

发布三个 Listing Tool:

- ``get_listing`` (v1):只读,输入为 ``store_ref`` + ``sku``;
- ``publish_listing`` (v1):把刊登置为 ``PUBLISHED``,输入为 ``store_ref`` + ``sku``;
- ``unpublish_listing`` (v1):把刊登置为 ``UNPUBLISHED``,输入为 ``store_ref`` +
  ``sku`` + 可选受控 ``reason``。

两个写 Tool 的 ``ToolBinding`` 需要请求级 Actor、Session 与平台 Gateway,不能由
模块级常量给出——因此只导出 Definition,由组合根用
:func:`build_publish_listing_binding` / :func:`build_unpublish_listing_binding`
显式装配(与既有写 Tool 的约定一致)。
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import p1_fields_for, product_registration
from agents.product_operations.listing.models import (
    GetListingInput,
    ListingPublicationResult,
    ListingSnapshot,
    PublishListingInput,
    UnpublishListingInput,
)
from agents.product_operations.listing.service import (
    PRODUCT_LISTING_WRITE_PERMISSION,
    PUBLISH_LISTING_VERSION,
    STABLE_KEY_PUBLISH_LISTING,
    STABLE_KEY_UNPUBLISH_LISTING,
    UNPUBLISH_LISTING_VERSION,
    ListingService,
)
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.pretool.profiles import PreToolProfileRegistration
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_GET_LISTING: Final = "get_listing"
GET_LISTING_VERSION: Final = 1


def _strict_input_schema(model: type[Any], *, additional_properties: bool) -> dict[str, Any]:
    """派生 Input JSON Schema;写 Tool 关闭额外字段(未允许字段在 Schema 层拒绝)。"""
    schema: dict[str, Any] = model.model_json_schema()
    schema["additionalProperties"] = additional_properties
    return schema


GET_LISTING_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_GET_LISTING,
    version=GET_LISTING_VERSION,
    description=(
        "读取授权店铺内单个 SKU 的 Listing 快照:标题、当前售价、币种、规范"
        "发布状态、Listing Version 与 issues;店铺用名称/别名引用。"
    ),
    input_json_schema=GetListingInput.model_json_schema(),
    output_json_schema=ListingSnapshot.model_json_schema(),
    effect=ToolEffect.READ,
    required_permissions=frozenset({"product.read"}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        GetListingInput.model_json_schema(), ListingSnapshot.model_json_schema()
    ),
)


async def get_listing(
    *, actor: ActorContext, session: AsyncSession, store_ref: str, sku: str
) -> ListingSnapshot:
    """经 :class:`ListingService` 读取;授权校验与店铺解析都在 Service 内。"""
    return await ListingService(session).get_listing(
        actor=actor, store_ref=store_ref, sku=sku
    )


GET_LISTING_BINDING: Final = ToolBinding(
    definition=GET_LISTING_DEFINITION,
    handler=get_listing,
    pretool_registration=product_registration(
        fields=p1_fields_for(STABLE_KEY_GET_LISTING),
        policy_mode="NONE",
        resource_mode="WORKSPACE",
    ),
)


PUBLISH_LISTING_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_PUBLISH_LISTING,
    version=PUBLISH_LISTING_VERSION,
    description=(
        "把授权店铺内指定 SKU 的 Listing 置为已发布(PUBLISHED)。输入为店铺引用"
        "与 SKU;Listing 标识、版本与幂等凭据由服务端补全;目标状态已由本 Tool "
        "固定,不需要也不接受模型填写;已经是已发布状态时按幂等 no-op 成功返回。"
    ),
    input_json_schema=_strict_input_schema(
        PublishListingInput, additional_properties=False
    ),
    output_json_schema=ListingPublicationResult.model_json_schema(),
    effect=ToolEffect.WRITE,
    required_permissions=frozenset({PRODUCT_LISTING_WRITE_PERMISSION}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        _strict_input_schema(PublishListingInput, additional_properties=False),
        ListingPublicationResult.model_json_schema(),
    ),
)


UNPUBLISH_LISTING_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_UNPUBLISH_LISTING,
    version=UNPUBLISH_LISTING_VERSION,
    description=(
        "把授权店铺内指定 SKU 的 Listing 置为取消发布(UNPUBLISHED),保留内部对象"
        "与历史记录。输入为店铺引用、SKU 与可选受控原因;取消发布是刊登决策,"
        "**不会**删除商品,也**不会**清零库存——库存为零不等价于取消发布。"
    ),
    input_json_schema=_strict_input_schema(
        UnpublishListingInput, additional_properties=False
    ),
    output_json_schema=ListingPublicationResult.model_json_schema(),
    effect=ToolEffect.WRITE,
    required_permissions=frozenset({PRODUCT_LISTING_WRITE_PERMISSION}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        _strict_input_schema(UnpublishListingInput, additional_properties=False),
        ListingPublicationResult.model_json_schema(),
    ),
)


def build_publish_listing_binding(
    *,
    handler: Any,
    pretool_registration: PreToolProfileRegistration,
    execution_profile: Any | None = None,
    write_adapter_capabilities: frozenset[str] = frozenset(),
) -> ToolBinding:
    """由组合根装配 ``publish_listing`` 的绑定(Handler 与 Profile 均为请求相关)。"""
    return ToolBinding(
        definition=PUBLISH_LISTING_DEFINITION,
        handler=handler,
        pretool_registration=pretool_registration,
        execution_profile=execution_profile,
        write_adapter_capabilities=write_adapter_capabilities,
    )


def build_unpublish_listing_binding(
    *,
    handler: Any,
    pretool_registration: PreToolProfileRegistration,
    execution_profile: Any | None = None,
    write_adapter_capabilities: frozenset[str] = frozenset(),
) -> ToolBinding:
    """由组合根装配 ``unpublish_listing`` 的绑定(Handler 与 Profile 均为请求相关)。"""
    return ToolBinding(
        definition=UNPUBLISH_LISTING_DEFINITION,
        handler=handler,
        pretool_registration=pretool_registration,
        execution_profile=execution_profile,
        write_adapter_capabilities=write_adapter_capabilities,
    )


__all__ = [
    "GET_LISTING_BINDING",
    "GET_LISTING_DEFINITION",
    "GET_LISTING_VERSION",
    "PUBLISH_LISTING_DEFINITION",
    "PUBLISH_LISTING_VERSION",
    "STABLE_KEY_GET_LISTING",
    "STABLE_KEY_PUBLISH_LISTING",
    "STABLE_KEY_UNPUBLISH_LISTING",
    "UNPUBLISH_LISTING_DEFINITION",
    "UNPUBLISH_LISTING_VERSION",
    "build_publish_listing_binding",
    "build_unpublish_listing_binding",
    "get_listing",
]
