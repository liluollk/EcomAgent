"""Listing 能力模块(P1 Task 5 查询;Task 8 发布状态写入)。

只负责 Listing:确定性读取、发布状态表达、发布/取消发布与回查。拥有自己的
``ListingGateway`` Port、``ListingPolicy`` 与应用服务;价格与库存分别属于
``pricing`` 与 ``inventory`` 模块,不在此处承载。
"""

from __future__ import annotations

from agents.product_operations.listing.gateway import (
    REASON_LISTING_NOT_FOUND,
    ChangePublicationCommand,
    ListingGateway,
    ListingPublicationSnapshot,
    ListingQuery,
    ListingUnavailable,
    ReconcileListingCommand,
    derive_idempotency_key,
)
from agents.product_operations.listing.models import (
    INVALID_UNPUBLISH_REASON,
    GetListingInput,
    ListingPublicationResult,
    ListingSnapshot,
    PublishListingInput,
    UnpublishListingInput,
    UnpublishReason,
)
from agents.product_operations.listing.policy import (
    ListingPolicy,
    ListingPolicyConfig,
    ListingPolicyDecision,
)
from agents.product_operations.listing.reconciliation import (
    ListingReconciler,
    build_reconcile_listing_command,
)
from agents.product_operations.listing.service import (
    OPERATION_TYPE,
    PRODUCT_DISABLED_ISSUE,
    PRODUCT_LISTING_WRITE_PERMISSION,
    STABLE_KEY_PUBLISH_LISTING,
    STABLE_KEY_UNPUBLISH_LISTING,
    ChangePublicationHandler,
    ListingService,
    PreparedChangePublication,
    PublicationState,
    PublishListingHandler,
    UnpublishListingHandler,
)
from agents.product_operations.listing.tools import (
    GET_LISTING_BINDING,
    GET_LISTING_DEFINITION,
    GET_LISTING_VERSION,
    PUBLISH_LISTING_DEFINITION,
    PUBLISH_LISTING_VERSION,
    STABLE_KEY_GET_LISTING,
    UNPUBLISH_LISTING_DEFINITION,
    UNPUBLISH_LISTING_VERSION,
    build_publish_listing_binding,
    build_unpublish_listing_binding,
    get_listing,
)


__all__ = [
    "GET_LISTING_BINDING",
    "GET_LISTING_DEFINITION",
    "GET_LISTING_VERSION",
    "INVALID_UNPUBLISH_REASON",
    "OPERATION_TYPE",
    "PRODUCT_DISABLED_ISSUE",
    "PRODUCT_LISTING_WRITE_PERMISSION",
    "PUBLISH_LISTING_DEFINITION",
    "PUBLISH_LISTING_VERSION",
    "REASON_LISTING_NOT_FOUND",
    "STABLE_KEY_GET_LISTING",
    "STABLE_KEY_PUBLISH_LISTING",
    "STABLE_KEY_UNPUBLISH_LISTING",
    "UNPUBLISH_LISTING_DEFINITION",
    "UNPUBLISH_LISTING_VERSION",
    "ChangePublicationCommand",
    "ChangePublicationHandler",
    "GetListingInput",
    "ListingGateway",
    "ListingPolicy",
    "ListingPolicyConfig",
    "ListingPolicyDecision",
    "ListingPublicationResult",
    "ListingPublicationSnapshot",
    "ListingQuery",
    "ListingReconciler",
    "ListingService",
    "ListingSnapshot",
    "ListingUnavailable",
    "PreparedChangePublication",
    "PublicationState",
    "PublishListingHandler",
    "PublishListingInput",
    "ReconcileListingCommand",
    "UnpublishListingHandler",
    "UnpublishListingInput",
    "UnpublishReason",
    "build_publish_listing_binding",
    "build_reconcile_listing_command",
    "build_unpublish_listing_binding",
    "derive_idempotency_key",
    "get_listing",
]
