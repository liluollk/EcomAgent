"""``get_listing`` 的 ToolDefinition / handler / binding(P1 Task 5;设计 §4.5)。

新 stable key ``get_listing``(版本 1),输入为 ``store_ref`` + ``sku``:内部
``store_id``/``listing_id`` 由 Service 解析,不出现在输入 Schema。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import p1_fields_for, product_registration
from agents.product_operations.listing.models import GetListingInput, ListingSnapshot
from agents.product_operations.listing.service import ListingService
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding
from runtime.capabilities.validation import compute_schema_hash
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_GET_LISTING: Final = "get_listing"
GET_LISTING_VERSION: Final = 1


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


__all__ = [
    "GET_LISTING_BINDING",
    "GET_LISTING_DEFINITION",
    "GET_LISTING_VERSION",
    "STABLE_KEY_GET_LISTING",
    "get_listing",
]
