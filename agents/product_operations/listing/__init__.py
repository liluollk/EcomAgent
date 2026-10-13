"""Listing 能力模块(P1 Task 5;设计 §4.5)。

只负责 Listing 的读取与发布状态表达:输入/输出模型、只读应用服务,以及
``get_listing`` 的 Tool 定义与绑定。发布/取消发布的写路径由 Task 8 增加。
"""

from __future__ import annotations

from agents.product_operations.listing.models import GetListingInput, ListingSnapshot
from agents.product_operations.listing.service import (
    PRODUCT_DISABLED_ISSUE,
    ListingService,
    PublicationState,
)
from agents.product_operations.listing.tools import (
    GET_LISTING_BINDING,
    GET_LISTING_DEFINITION,
    GET_LISTING_VERSION,
    STABLE_KEY_GET_LISTING,
    get_listing,
)


__all__ = [
    "GET_LISTING_BINDING",
    "GET_LISTING_DEFINITION",
    "GET_LISTING_VERSION",
    "PRODUCT_DISABLED_ISSUE",
    "STABLE_KEY_GET_LISTING",
    "GetListingInput",
    "ListingService",
    "ListingSnapshot",
    "PublicationState",
    "get_listing",
]
