"""OperationStore JSONL 记录与重建测试（按 session 隔离）。"""

from decimal import Decimal

import pytest

from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceError,
    PriceErrorCode,
    PricePlatform,
    PriceWriteReceipt,
    PriceVerification,
    ProductRef,
    ProductSnapshot,
)
from execution.price_change import PriceChangeCoordinator, PriceChangeState
from session.operation_store import OperationStore


class FakeCostProvider:
    def get_cost_price(self, channel, sku):
        return 50.0


class FakePlatform:
    def __init__(self, *, write_behavior="success", verify_consistent=True):
        self.platform = "mock"
        self.write_behavior = write_behavior
        self.verify_consistent = verify_consistent
        self.apply_calls = 0

    async def query_snapshot(self, ref: ProductRef) -> ProductSnapshot:
        return ProductSnapshot(product_ref=ref, current_price=Decimal("50"), stock=9)

    async def apply_price(self, command: PriceChangeCommand, *, idempotency_key=None):
        self.apply_calls += 1
        if self.write_behavior == "unknown":
            raise PriceError(PriceErrorCode.UNKNOWN_OUTCOME, "超时", side_effect_possible=True)
        return PriceWriteReceipt(product_ref=command.product_ref, applied_price=command.target_price)

    async def verify_price(self, ref: ProductRef, expected_price: Decimal) -> PriceVerification:
        return PriceVerification(
            product_ref=ref, expected_price=expected_price,
            observed_price=expected_price if self.verify_consistent else expected_price + Decimal("1"),
            consistent=self.verify_consistent,
        )


def make_command(operation_id="op-1", platform="mock"):
    ref = ProductRef(platform=platform, shop_id="s1", product_id="p1", sku_id="sku1")
    return PriceChangeCommand(
        operation_id=operation_id, product_ref=ref,
        target_price=Decimal("100"), requester="ops", reason="调价",
    )


async def run(coord, command, approve=True):
    return await coord.execute(command, lambda ev: approve)


# ---------------------------------------------------------------------------
# 写入与读取
# ---------------------------------------------------------------------------
def test_append_writes_jsonl_and_load(tmp_path):
    store = OperationStore(str(tmp_path))
    store.append("sess-a", {"type": "operation_created", "operation_id": "op-1"})
    store.append("sess-a", {"type": "state_changed", "to_state": "PRECHECKED", "operation_id": "op-1"})
    records = store.load("sess-a")
    assert len(records) == 2
    assert records[0]["type"] == "operation_created"
    assert records[0]["session_id"] == "sess-a"
    assert records[1]["to_state"] == "PRECHECKED"


def test_per_session_isolation(tmp_path):
    store = OperationStore(str(tmp_path))
    store.append("sess-a", {"type": "operation_created", "operation_id": "op-a"})
    store.append("sess-b", {"type": "operation_created", "operation_id": "op-b"})
    assert store.operations_for_session("sess-a") == ["op-a"]
    assert store.operations_for_session("sess-b") == ["op-b"]
    assert store.load("sess-missing") == []


# ---------------------------------------------------------------------------
# 从 JSONL 重建操作
# ---------------------------------------------------------------------------
async def test_rebuild_successful_operation(tmp_path):
    store = OperationStore(str(tmp_path))
    coord = PriceChangeCoordinator(
        FakePlatform(), session_id="sess-r", operation_store=store,
        cost_provider=FakeCostProvider(),
    )
    op = await run(coord, make_command("op-r"))
    assert op.state is PriceChangeState.SUCCEEDED

    rebuilt = store.rebuild_operation("sess-r", "op-r")
    assert rebuilt is not None
    assert rebuilt.operation_id == "op-r"
    assert rebuilt.state is PriceChangeState.SUCCEEDED
    assert rebuilt.command.target_price == Decimal("100")
    assert rebuilt.command.product_ref.sku_id == "sku1"
    assert rebuilt.idempotency_key == "price_op-r"
    assert "SUCCEEDED" in rebuilt.trail


async def test_rebuild_unknown_outcome_trail(tmp_path):
    store = OperationStore(str(tmp_path))
    coord = PriceChangeCoordinator(
        FakePlatform(write_behavior="unknown"), session_id="sess-u",
        operation_store=store, cost_provider=FakeCostProvider(),
    )
    op = await run(coord, make_command("op-u"))
    assert op.state is PriceChangeState.SUCCEEDED

    rebuilt = store.rebuild_operation("sess-u", "op-u")
    assert rebuilt is not None
    assert rebuilt.state is PriceChangeState.SUCCEEDED
    assert "UNKNOWN_OUTCOME" in rebuilt.trail
    # 重建出的轨迹与运行时一致
    assert rebuilt.trail == op.trail


def test_rebuild_missing_returns_none(tmp_path):
    store = OperationStore(str(tmp_path))
    assert store.rebuild_operation("sess-x", "nope") is None


# ---------------------------------------------------------------------------
# 重建后可直接用协调器恢复（复用同一 operation_id 与幂等键）
# ---------------------------------------------------------------------------
async def test_rebuild_then_resume_reuses_key(tmp_path):
    store = OperationStore(str(tmp_path))
    platform = FakePlatform(write_behavior="unknown")
    coord = PriceChangeCoordinator(
        platform, session_id="sess-z", operation_store=store,
        cost_provider=FakeCostProvider(),
    )
    op = await run(coord, make_command("op-z"))
    assert op.state is PriceChangeState.SUCCEEDED
    assert platform.apply_calls == 1

    rebuilt = store.rebuild_operation("sess-z", "op-z")
    # 重建出的操作保持固定幂等键；再次恢复不得产生第二次写入
    assert rebuilt.idempotency_key == "price_op-z"
    await coord.resume(rebuilt)
    assert platform.apply_calls == 1
