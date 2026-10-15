"""ProductOperations 模块化回查分发测试。"""

from __future__ import annotations

import uuid

import pytest

from agents.product_operations.inventory.service import OPERATION_TYPE as INVENTORY
from agents.product_operations.listing.service import OPERATION_TYPE as LISTING
from agents.product_operations.pricing.service import OPERATION_TYPE as PRICING
from composition.product_operations_recovery import build_reconciler_registry


class _Gateway:
    pass


def test_reconciler_registry_contains_all_p1_write_operation_types() -> None:
    registry = build_reconciler_registry(
        pricing_gateway=_Gateway(),
        inventory_gateway=_Gateway(),
        listing_gateway=_Gateway(),
        workspace_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
    )

    assert set(registry.operation_types()) == {PRICING, INVENTORY, LISTING}


def test_reconciler_registry_rejects_unknown_operation_type() -> None:
    registry = build_reconciler_registry(
        pricing_gateway=_Gateway(),
        inventory_gateway=_Gateway(),
        listing_gateway=_Gateway(),
        workspace_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
    )

    with pytest.raises(ValueError, match="unregistered operation type"):
        registry.require("UNKNOWN_OPERATION")
