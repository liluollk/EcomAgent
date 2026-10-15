"""ProductOperations Agent 与 Skill 共享的业务 Tool stable key 集合。"""

from typing import Final

from agents.product_operations.catalog.tools import STABLE_KEY_SEARCH_PRODUCTS
from agents.product_operations.inventory.service import (
    STABLE_KEY_ADJUST_INVENTORY_QUANTITY,
)
from agents.product_operations.inventory.tools import STABLE_KEY_GET_INVENTORY_LEVEL
from agents.product_operations.listing.service import (
    STABLE_KEY_PUBLISH_LISTING,
    STABLE_KEY_UNPUBLISH_LISTING,
)
from agents.product_operations.listing.tools import STABLE_KEY_GET_LISTING
from agents.product_operations.pricing.service import STABLE_KEY_SET_LISTING_PRICE
from agents.product_operations.pricing.tools import STABLE_KEY_COMPARE_LISTING_PRICES


P1_BUSINESS_TOOL_KEYS: Final = frozenset(
    {
        STABLE_KEY_SEARCH_PRODUCTS,
        STABLE_KEY_GET_LISTING,
        STABLE_KEY_COMPARE_LISTING_PRICES,
        STABLE_KEY_SET_LISTING_PRICE,
        STABLE_KEY_GET_INVENTORY_LEVEL,
        STABLE_KEY_ADJUST_INVENTORY_QUANTITY,
        STABLE_KEY_PUBLISH_LISTING,
        STABLE_KEY_UNPUBLISH_LISTING,
    }
)


__all__ = ["P1_BUSINESS_TOOL_KEYS"]
