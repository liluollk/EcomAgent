"""Mock Commerce Adapter 测试(计划 03 Task 5;架构文档 §7 写执行与回查)。

覆盖:
- 计划原文:response_lost_after_commit 后 execute 返回 UNKNOWN,回查
  APPLIED,逻辑写计数为 1(写入生效但响应丢失不产生二次写);
- 幂等:同 operation_id 重复 execute 回放上次回执,不产生第二次逻辑写,
  Listing version 只增一次;
- CAS:expected_version 过期时写被拒(确定失败,不 UNKNOWN,零逻辑写);
- CAS 成功:Listing version+1、价格落到权威状态(listings 表);
- 故障注入:timeout_before_write(写前 UNKNOWN 且零写)、
  temporary_reconcile_unknown(首次回查 UNKNOWN,之后返回真实结论)、
  permanent_failure(始终确定失败,不 UNKNOWN);
- 故障注入确定性(无随机):同一脚本在全新 Adapter 上重复执行结果一致;
- 无故障正常路径:APPLIED,回执携带前后价格与最终版本;
- 平台侧命令校验:币种不一致与 Listing 不存在时确定失败且零写。
"""

import dataclasses
import uuid
from decimal import Decimal

from fixtures.product_operations import ProductOperationsSeeder
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.commerce.faults import FaultInjector
from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.protocol import ExecutionStatus, UpdatePriceCommand
from agents.product_operations.persistence.models import Listing
from agents.product_operations.persistence.repository import ProductRepository
from runtime.execution.reconciliation import ReconcileResult


def update_command(
    listing: Listing,
    *,
    operation_id: str,
    target_price: Decimal = Decimal("99.00"),
    expected_version: int | None = None,
) -> UpdatePriceCommand:
    """由 Listing 行构造服务端补全后的改价命令(模型只提供目标价)。"""
    return UpdatePriceCommand(
        operation_id=operation_id,
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        target_price=target_price,
        currency=listing.currency,
        expected_version=(
            listing.version if expected_version is None else expected_version
        ),
    )


async def test_response_loss_does_not_duplicate_price_write(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    mock_adapter.faults.inject("response_lost_after_commit")
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == "UNKNOWN"
    reconciled = await mock_adapter.reconcile("op-1", expected_price=Decimal("99.00"))
    assert reconciled == ReconcileResult.APPLIED
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_duplicate_operation_id_replays_receipt_without_second_write(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """幂等:同 operation_id 重复 execute 回放同一回执,零额外逻辑写。"""
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == ExecutionStatus.APPLIED

    replay = await mock_adapter.execute(update_command(listing, operation_id="op-1"))

    assert replay == first
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_duplicate_submit_writes_listing_version_only_once(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
    product_repository: ProductRepository,
) -> None:
    """重复提交同一 operation_id:Listing version 只增一次。"""
    previous_version = listing.version
    await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    await mock_adapter.execute(update_command(listing, operation_id="op-1"))

    row = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert row is not None
    assert row.version == previous_version + 1


async def test_cas_version_conflict_rejects_write_without_logical_write(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
    product_repository: ProductRepository,
) -> None:
    """expected_version 过期:确定失败(非 UNKNOWN),权威状态不被覆盖。"""
    receipt = await mock_adapter.execute(
        update_command(
            listing, operation_id="op-stale", expected_version=listing.version + 1
        )
    )

    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.reason_code == "VERSION_CONFLICT"
    assert await mock_adapter.count_logical_writes("op-stale") == 0

    row = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert row is not None
    assert row.version == listing.version
    assert row.current_price == Decimal("128.00")


async def test_cas_success_increments_version_and_lands_price(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
    product_repository: ProductRepository,
) -> None:
    """CAS 命中:version+1,价格落到 listings 权威状态,回查确认 APPLIED。"""
    previous_version = listing.version
    receipt = await mock_adapter.execute(update_command(listing, operation_id="op-1"))

    assert receipt.status == ExecutionStatus.APPLIED
    assert receipt.before_price == Decimal("128.00")
    assert receipt.after_price == Decimal("99.00")
    assert receipt.listing_version == previous_version + 1

    row = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert row is not None
    assert row.current_price == Decimal("99.00")
    assert row.version == previous_version + 1
    assert await mock_adapter.reconcile("op-1", expected_price=Decimal("99.00")) == (
        ReconcileResult.APPLIED
    )


async def test_timeout_before_write_returns_unknown_without_write(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
    product_repository: ProductRepository,
) -> None:
    """写前超时:UNKNOWN 且请求未达平台写路径(零逻辑写、状态不变)。"""
    mock_adapter.faults.inject("timeout_before_write")

    receipt = await mock_adapter.execute(update_command(listing, operation_id="op-1"))

    assert receipt.status == ExecutionStatus.UNKNOWN
    assert receipt.reason_code == "TIMEOUT_BEFORE_WRITE"
    assert await mock_adapter.count_logical_writes("op-1") == 0

    row = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert row is not None
    assert row.version == listing.version
    assert row.current_price == Decimal("128.00")


async def test_reconcile_unknown_is_temporary_then_real_answer(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """temporary_reconcile_unknown:首次回查 UNKNOWN,之后返回真实结论。"""
    mock_adapter.faults.inject("response_lost_after_commit")
    await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    mock_adapter.faults.clear()
    mock_adapter.faults.inject("temporary_reconcile_unknown")

    first = await mock_adapter.reconcile("op-1", expected_price=Decimal("99.00"))
    second = await mock_adapter.reconcile("op-1", expected_price=Decimal("99.00"))
    third = await mock_adapter.reconcile("op-1", expected_price=Decimal("99.00"))

    assert first == ReconcileResult.UNKNOWN
    assert second == ReconcileResult.APPLIED
    assert third == ReconcileResult.APPLIED


async def test_permanent_failure_is_definite_and_never_unknown(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
    product_repository: ProductRepository,
) -> None:
    """permanent_failure:每次 execute 都确定失败(非 UNKNOWN)且零写。"""
    mock_adapter.faults.inject("permanent_failure")

    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    second = await mock_adapter.execute(update_command(listing, operation_id="op-2"))

    assert first.status == ExecutionStatus.FAILED
    assert first.reason_code == "PLATFORM_FAILURE"
    assert second.status == ExecutionStatus.FAILED
    assert await mock_adapter.count_logical_writes("op-1") == 0
    assert await mock_adapter.count_logical_writes("op-2") == 0

    row = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert row is not None
    assert row.version == listing.version


async def test_fault_injection_reproduces_identical_outcomes(
    product_seeder: ProductOperationsSeeder, db_session: AsyncSession
) -> None:
    """故障注入无随机:同一脚本在两个全新 Adapter 上逐项结果一致。"""
    outcomes: list[tuple[str, str, str, int]] = []
    for index in range(2):
        seed = await product_seeder.seed()
        adapter = MockCommerceAdapter(db_session)
        operation_id = f"op-deterministic-{index}"

        adapter.faults.inject("response_lost_after_commit")
        receipt = await adapter.execute(
            update_command(seed.listing, operation_id=operation_id)
        )
        adapter.faults.inject("temporary_reconcile_unknown")
        first = await adapter.reconcile(operation_id, expected_price=Decimal("99.00"))
        second = await adapter.reconcile(operation_id, expected_price=Decimal("99.00"))
        outcomes.append(
            (
                receipt.status.value,
                first.value,
                second.value,
                await adapter.count_logical_writes(operation_id),
            )
        )

    assert outcomes[0] == outcomes[1] == ("UNKNOWN", "UNKNOWN", "APPLIED", 1)


async def test_execute_without_faults_applies_write(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """无故障正常路径:APPLIED 回执携带幂等键与前后价格。"""
    receipt = await mock_adapter.execute(update_command(listing, operation_id="op-1"))

    assert receipt.status == ExecutionStatus.APPLIED
    assert receipt.operation_id == "op-1"
    assert receipt.idempotency_key == "update_price:op-1"
    assert receipt.before_price == Decimal("128.00")
    assert receipt.after_price == Decimal("99.00")
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_currency_mismatch_is_rejected_deterministically(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """平台侧币种校验:币种不一致确定失败且零逻辑写。"""
    mismatched = dataclasses.replace(
        update_command(listing, operation_id="op-usd"), currency="USD"
    )

    receipt = await mock_adapter.execute(mismatched)

    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.reason_code == "CURRENCY_MISMATCH"
    assert await mock_adapter.count_logical_writes("op-usd") == 0


async def test_unknown_listing_fails_without_write(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """Listing 不存在(或跨 Workspace):确定失败且零逻辑写。"""
    missing = dataclasses.replace(
        update_command(listing, operation_id="op-missing"), workspace_id=uuid.uuid4()
    )

    receipt = await mock_adapter.execute(missing)

    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.reason_code == "LISTING_NOT_FOUND"
    assert await mock_adapter.count_logical_writes("op-missing") == 0


def test_fault_injector_rejects_unknown_fault_name() -> None:
    injector = FaultInjector()
    try:
        injector.inject("not_a_fault")
    except ValueError as error:
        assert "not_a_fault" in str(error)
    else:
        raise AssertionError("inject must reject unknown fault names")
