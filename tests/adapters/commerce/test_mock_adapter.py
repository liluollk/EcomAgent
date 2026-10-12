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
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from functools import partial

from contracts import (
    InventoryContractEnv,
    ListingContractEnv,
    PriceContractEnv,
    assert_inventory_gateway_contract,
    assert_listing_gateway_contract,
    assert_price_gateway_contract,
)
from fixtures.product_operations import ProductOperationsSeed, ProductOperationsSeeder
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.commerce.faults import RESPONSE_LOST_AFTER_COMMIT, FaultInjector
from adapters.commerce.mock_adapter import (
    MockCommerceAdapter,
    _inventory_operation_intent,
    _price_operation_intent,
    _publication_operation_intent,
)
from adapters.commerce.protocol import (
    REASON_CURRENCY_MISMATCH,
    REASON_IDEMPOTENCY_CONFLICT,
    REASON_LISTING_NOT_FOUND,
    REASON_PLATFORM_FAILURE,
    REASON_VERSION_CONFLICT,
    CommerceAdapter,
    ExecutionStatus,
    ReconciliationCommand,
    UpdatePriceCommand,
    is_retryable_reason_code,
)
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    ReconcileInventoryCommand,
)
from agents.product_operations.inventory.service import (
    OPERATION_TYPE as INVENTORY_OPERATION_TYPE,
)
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingQuery,
    ReconcileListingCommand,
)
from agents.product_operations.listing.service import (
    OPERATION_TYPE as LISTING_OPERATION_TYPE,
)
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.pricing.service import (
    OPERATION_TYPE as PRICING_OPERATION_TYPE,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.execution.reconciliation import ReconcileResult


#: 用例默认目标价,与 ``update_command`` 的默认值保持一致。
TARGET_PRICE = Decimal("99.00")


async def test_logical_write_count_is_test_observation_not_adapter_contract(
    mock_adapter: MockCommerceAdapter,
) -> None:
    assert "count_logical_writes" not in CommerceAdapter.__dict__
    assert await mock_adapter.count_logical_writes("missing") == 0


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
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        target_price=target_price,
        currency=listing.currency,
        expected_version=(
            listing.version if expected_version is None else expected_version
        ),
    )


def reconciliation_command(
    listing: Listing, operation_id: str, expected_price: Decimal
) -> ReconciliationCommand:
    return ReconciliationCommand(
        operation_id=operation_id,
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        expected_price=expected_price,
        currency=listing.currency,
    )


async def add_twin_listing(
    db_session: AsyncSession,
    seed: ProductOperationsSeed,
    *,
    current_price: Decimal = TARGET_PRICE,
) -> Listing:
    """同 Workspace/店铺下再建一个 Listing(独立商品),用于"错目标"反例。

    价格默认等于目标价,构成"只看价格/数量就会被骗过"的陷阱。
    """
    twin_product = Product(
        id=uuid.uuid4(),
        workspace_id=seed.workspace.id,
        sku=f"SKU-{uuid.uuid4()}",
        title="twin-product",
    )
    db_session.add(twin_product)
    await db_session.flush()
    twin_listing = Listing(
        id=uuid.uuid4(),
        workspace_id=seed.workspace.id,
        store_id=seed.store.id,
        product_id=twin_product.id,
        external_product_ref=f"item-{uuid.uuid4()}",
        currency="CNY",
        current_price=current_price,
        cost_price=Decimal("88.00"),
    )
    db_session.add(twin_listing)
    await db_session.flush()
    return twin_listing


def gateway_price_command(
    listing: Listing,
    *,
    operation_id: str,
    target_price: Decimal = TARGET_PRICE,
) -> SetListingPriceCommand:
    """由 Listing 行构造 P1 门面改价命令(服务端补全后的可信事实)。"""
    return SetListingPriceCommand(
        operation_id=operation_id,
        idempotency_key=f"set_listing_price:{operation_id}",
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        sku="SKU-1",
        target_price=target_price,
        currency=listing.currency,
        expected_version=listing.version,
    )


def gateway_publication_command(
    listing: Listing, *, operation_id: str
) -> ChangePublicationCommand:
    """由 Listing 行构造 P1 门面取消发布命令(方向由方法固定,命令不带目标状态)。"""
    return ChangePublicationCommand(
        operation_id=operation_id,
        idempotency_key=f"change_listing_publication:{operation_id}",
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        sku="SKU-1",
        reason=None,
        expected_version=listing.version,
    )


def publication_query(listing: Listing) -> ListingQuery:
    return ListingQuery(
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
    )


def price_query(listing: Listing) -> PriceQuery:
    return PriceQuery(
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        currency=listing.currency,
    )


async def test_response_loss_does_not_duplicate_price_write(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    mock_adapter.faults.inject("response_lost_after_commit")
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == "UNKNOWN"
    reconciled = await mock_adapter.reconcile(
        reconciliation_command(listing, "op-1", Decimal("99.00"))
    )
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


async def test_reconcile_rejects_other_listing_with_same_price(
    mock_adapter: MockCommerceAdapter,
    product_seed: ProductOperationsSeed,
    product_repository: ProductRepository,
    db_session: AsyncSession,
) -> None:
    """错 Listing 但价格恰好相同 → 不得判定 APPLIED(回查绑定资源身份)。"""
    first = await mock_adapter.execute(
        update_command(product_seed.listing, operation_id="op-1")
    )
    assert first.status == ExecutionStatus.APPLIED
    assert first.after_price == TARGET_PRICE

    twin_listing = await add_twin_listing(db_session, product_seed)

    # 自证陷阱有效:另一个 Listing 的价格确实等于目标价,只看价格的旧实现会被骗过。
    twin = await product_repository.get_listing(
        product_seed.workspace.id, twin_listing.id
    )
    assert twin is not None and twin.current_price == TARGET_PRICE

    misrouted = ReconciliationCommand(
        operation_id="op-1",
        workspace_id=product_seed.workspace.id,
        store_id=product_seed.store.id,
        listing_id=twin_listing.id,
        external_store_ref=str(product_seed.store.id),
        external_product_ref=twin_listing.external_product_ref,
        expected_price=TARGET_PRICE,
        currency="CNY",
    )
    assert await mock_adapter.reconcile(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reused_operation_id_on_another_listing_fails_closed(
    mock_adapter: MockCommerceAdapter,
    product_seed: ProductOperationsSeed,
    db_session: AsyncSession,
) -> None:
    """同一 operation_id 作用到另一个 Listing → 确定失败,不回放旧成功回执。

    写路径必须抵御错路由:同一个 ``operation_id`` 换了目标资源时,回放旧回执
    会把"另一处已成功"伪装成"本次已生效"。
    """
    first = await mock_adapter.execute(
        update_command(product_seed.listing, operation_id="op-1")
    )
    assert first.status == ExecutionStatus.APPLIED

    twin_listing = await add_twin_listing(
        db_session, product_seed, current_price=Decimal("77.00")
    )
    conflicting = await mock_adapter.execute(
        update_command(twin_listing, operation_id="op-1")
    )

    assert conflicting.status == ExecutionStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert not is_retryable_reason_code(conflicting.reason_code)
    # 冲突不得产生第二次逻辑写,也不得改动另一个 Listing。
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_reused_operation_id_with_different_target_price_fails_closed(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """同一个 operation_id 在同一 Listing 上改不同的价 → 确定失败,不回放旧成功。

    只比较资源身份的旧实现会把这个"另一个业务动作"回放成本次成功。
    """
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == ExecutionStatus.APPLIED

    conflicting = await mock_adapter.execute(
        update_command(listing, operation_id="op-1", target_price=Decimal("199.00"))
    )

    assert conflicting.status == ExecutionStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_reused_operation_id_with_different_currency_fails_closed(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """同一个 operation_id 换币种 → 在币种校验之前就确定失败,不回放旧成功。"""
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == ExecutionStatus.APPLIED

    conflicting = await mock_adapter.execute(
        dataclasses.replace(update_command(listing, operation_id="op-1"), currency="USD")
    )

    assert conflicting.status == ExecutionStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_reused_operation_id_across_capabilities_price_then_publication(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
) -> None:
    """同一 Listing、同一 operation_id:先改价再改发布状态 → 跨能力复用确定失败。

    改价与刊登共享 ``workspace_id + listing_id`` 资源身份,指纹中段同形;旧实现只靠
    后续字段取值域恰好不相交来避免碰撞。显式能力前缀让跨能力复用在**结构上**即冲突:
    第二个能力不得回放第一个能力的成功回执,也不得改动平台状态。
    """
    operation_id = "op-cross-price-then-publication"
    price_receipt = await mock_adapter.set_price(
        gateway_price_command(listing, operation_id=operation_id)
    )
    assert price_receipt.status is WriteStatus.APPLIED

    # 取消发布会真正翻转 listings.status,因此"未被改动"是可观测的强断言。
    conflict = await mock_adapter.unpublish_listing(
        gateway_publication_command(listing, operation_id=operation_id)
    )

    assert conflict.status is WriteStatus.FAILED
    assert conflict.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await mock_adapter.count_logical_writes(operation_id) == 1
    snapshot = await mock_adapter.get_listing(publication_query(listing))
    assert snapshot.publication_state is ListingStatus.PUBLISHED


async def test_reused_operation_id_across_capabilities_publication_then_price(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
) -> None:
    """反向:先取消发布再改价,复用同一 operation_id → 同样确定失败,价格不变。"""
    operation_id = "op-cross-publication-then-price"
    before_price = (await mock_adapter.get_price(price_query(listing))).price
    publication_receipt = await mock_adapter.unpublish_listing(
        gateway_publication_command(listing, operation_id=operation_id)
    )
    assert publication_receipt.status is WriteStatus.APPLIED

    conflict = await mock_adapter.set_price(
        gateway_price_command(
            listing, operation_id=operation_id, target_price=Decimal("199.00")
        )
    )

    assert conflict.status is WriteStatus.FAILED
    assert conflict.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await mock_adapter.count_logical_writes(operation_id) == 1
    assert (await mock_adapter.get_price(price_query(listing))).price == before_price


async def test_operation_intents_are_explicitly_namespaced_by_capability(
    product_seed: ProductOperationsSeed,
) -> None:
    """三个能力指纹的首元素必须是本能力的受控 ``operation_type``。

    跨能力复用 ``operation_id`` 之所以一定冲突,不能只靠"后续字段取值域恰好不相交"
    这种偶然安全——旧实现里价格与刊登指纹中段同形(``external_store_ref``、
    ``external_product_ref``),首元素都是 ``workspace_id``(UUID)。本测试直接检查
    首元素,因此去掉能力前缀后必然失败,行为测试做不到这一点。
    """
    listing = product_seed.listing
    price_intent = _price_operation_intent(
        gateway_price_command(listing, operation_id="op-price")
    )
    publication_intent = _publication_operation_intent(
        gateway_publication_command(listing, operation_id="op-listing"),
        ListingStatus.UNPUBLISHED,
    )
    inventory_intent = _inventory_operation_intent(
        AdjustInventoryCommand(
            operation_id="op-inventory",
            idempotency_key="adjust_inventory_quantity:op-inventory",
            workspace_id=product_seed.workspace.id,
            store_id=product_seed.store.id,
            location_id=product_seed.fulfillment_location.id,
            product_id=product_seed.product.id,
            external_store_ref=product_seed.store.external_store_ref,
            external_location_ref=(
                product_seed.fulfillment_location.external_location_ref
            ),
            external_product_ref=product_seed.listing.external_product_ref,
            sku=product_seed.product.sku,
            delta=1,
            reason="MANUAL",
            expected_version=product_seed.inventory_level.version,
            quantity_before=product_seed.inventory_level.available_quantity,
        )
    )

    assert price_intent[0] == PRICING_OPERATION_TYPE
    assert inventory_intent[0] == INVENTORY_OPERATION_TYPE
    assert publication_intent[0] == LISTING_OPERATION_TYPE
    assert len({price_intent[0], inventory_intent[0], publication_intent[0]}) == 3


async def test_duplicate_submit_with_reread_version_still_replays(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    """对照面:意图相同的重复提交(仅 expected_version 因重读 +1)仍回放旧回执。

    ``expected_version`` 属于"基于哪次读取"的乐观锁版本,不属于业务意图;
    纳入指纹会把正常重复提交误判成冲突。
    """
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == ExecutionStatus.APPLIED

    replay = await mock_adapter.execute(
        update_command(listing, operation_id="op-1", expected_version=listing.version + 1)
    )

    assert replay == first
    assert await mock_adapter.count_logical_writes("op-1") == 1



async def test_cas_version_conflict_rejects_write_without_logical_write(
    mock_adapter: MockCommerceAdapter,
    listing: Listing,
    product_repository: ProductRepository,
) -> None:
    """expected_version 过期:确定失败(非 UNKNOWN),权威状态不被覆盖。

    同 ``operation_id`` 下 ``expected_version`` 不允许改写,平台也按该
    ``operation_id`` 记录了失败回执,因此该失败**不可重试**(重试只会回放旧结论)。
    """
    receipt = await mock_adapter.execute(
        update_command(
            listing, operation_id="op-stale", expected_version=listing.version + 1
        )
    )

    assert receipt.status == ExecutionStatus.FAILED
    assert receipt.reason_code == REASON_VERSION_CONFLICT
    assert receipt.retryable is False
    assert await mock_adapter.count_logical_writes("op-stale") == 0

    row = await product_repository.get_listing(listing.workspace_id, listing.id)
    assert row is not None
    assert row.version == listing.version
    assert row.current_price == Decimal("128.00")


def test_definite_failures_are_never_declared_retryable() -> None:
    """确定失败原因码都不会被标记为可重试(当前可重试集合为空)。

    ``PLATFORM_FAILURE`` 由持续不可用产生,``VERSION_CONFLICT`` 无法在同一
    ``operation_id`` 下改写目标版本:两者重试必然得到同一回执,不得进入
    重排队循环。
    """
    for reason_code in (
        REASON_PLATFORM_FAILURE,
        REASON_VERSION_CONFLICT,
        REASON_LISTING_NOT_FOUND,
        REASON_CURRENCY_MISMATCH,
        None,
    ):
        assert is_retryable_reason_code(reason_code) is False


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
    assert await mock_adapter.reconcile(
        reconciliation_command(listing, "op-1", Decimal("99.00"))
    ) == (
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

    command = reconciliation_command(listing, "op-1", Decimal("99.00"))
    first = await mock_adapter.reconcile(command)
    second = await mock_adapter.reconcile(command)
    third = await mock_adapter.reconcile(command)

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
    assert first.reason_code == REASON_PLATFORM_FAILURE
    # 持续不可用不是"暂时不可用":不得交给 Runtime 重排队。
    assert first.retryable is False
    assert second.status == ExecutionStatus.FAILED
    assert second.retryable is False
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
        command = reconciliation_command(seed.listing, operation_id, Decimal("99.00"))
        first = await adapter.reconcile(command)
        second = await adapter.reconcile(command)
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


# ---- 计划 P1 Task 9:能力契约套件(价格 / 库存 / 刊登) ----


@contextmanager
def _lose_response(adapter: MockCommerceAdapter) -> Iterator[None]:
    """测试侧的"写后丢响应":进入时注入故障,退出时清除。"""
    adapter.faults.inject(RESPONSE_LOST_AFTER_COMMIT)
    try:
        yield
    finally:
        adapter.faults.clear(RESPONSE_LOST_AFTER_COMMIT)


def _price_contract_env(
    adapter: MockCommerceAdapter,
    seed: ProductOperationsSeed,
    repository: ProductRepository,
) -> PriceContractEnv:
    """把种子行与 Mock Adapter 接到价格契约上。"""

    async def read_state() -> tuple[Decimal, int]:
        row = await repository.get_listing(seed.workspace.id, seed.listing.id)
        if row is None:
            raise LookupError("seeded listing disappeared from the platform state")
        return row.current_price, row.version

    def build_write(
        operation_id: str, expected_version: int, target_price: Decimal
    ) -> SetListingPriceCommand:
        return dataclasses.replace(
            gateway_price_command(
                seed.listing, operation_id=operation_id, target_price=target_price
            ),
            expected_version=expected_version,
        )

    def build_reconcile(
        operation_id: str, expected_price: Decimal
    ) -> ReconcilePriceCommand:
        return ReconcilePriceCommand(
            operation_id=operation_id,
            workspace_id=seed.workspace.id,
            store_id=seed.store.id,
            listing_id=seed.listing.id,
            external_store_ref=str(seed.store.id),
            external_product_ref=seed.listing.external_product_ref,
            expected_price=expected_price,
            currency=seed.listing.currency,
        )

    return PriceContractEnv(
        gateway=adapter,
        build_write=build_write,
        build_reconcile=build_reconcile,
        read_state=read_state,
        logical_writes=adapter.count_logical_writes,
        lose_response=partial(_lose_response, adapter),
        initial_price=seed.listing.current_price,
        target_price=TARGET_PRICE,
        second_target_price=Decimal("55.00"),
        initial_version=seed.listing.version,
    )


def _inventory_contract_env(
    adapter: MockCommerceAdapter,
    seed: ProductOperationsSeed,
    repository: ProductRepository,
) -> InventoryContractEnv:
    """把种子行与 Mock Adapter 接到库存契约上。"""

    async def read_state() -> tuple[int, int]:
        row = await repository.get_inventory_level(
            seed.workspace.id, seed.fulfillment_location.id, seed.product.id
        )
        if row is None:
            raise LookupError("seeded inventory level disappeared from the platform state")
        return row.available_quantity, row.version

    def build_write(
        operation_id: str, expected_version: int, delta: int, quantity_before: int
    ) -> AdjustInventoryCommand:
        return AdjustInventoryCommand(
            operation_id=operation_id,
            idempotency_key=f"adjust_inventory_quantity:{operation_id}",
            workspace_id=seed.workspace.id,
            store_id=seed.store.id,
            location_id=seed.fulfillment_location.id,
            product_id=seed.product.id,
            external_store_ref=seed.store.external_store_ref,
            external_location_ref=seed.fulfillment_location.external_location_ref,
            external_product_ref=seed.listing.external_product_ref,
            sku=seed.product.sku,
            delta=delta,
            reason="MANUAL",
            expected_version=expected_version,
            quantity_before=quantity_before,
        )

    def build_reconcile(
        operation_id: str, quantity_before: int, expected_quantity_after: int
    ) -> ReconcileInventoryCommand:
        return ReconcileInventoryCommand(
            operation_id=operation_id,
            workspace_id=seed.workspace.id,
            store_id=seed.store.id,
            location_id=seed.fulfillment_location.id,
            product_id=seed.product.id,
            external_store_ref=seed.store.external_store_ref,
            external_location_ref=seed.fulfillment_location.external_location_ref,
            external_product_ref=seed.listing.external_product_ref,
            quantity_before=quantity_before,
            expected_quantity_after=expected_quantity_after,
        )

    return InventoryContractEnv(
        gateway=adapter,
        build_write=build_write,
        build_reconcile=build_reconcile,
        read_state=read_state,
        logical_writes=adapter.count_logical_writes,
        lose_response=partial(_lose_response, adapter),
        initial_quantity=seed.inventory_level.available_quantity,
        first_delta=7,
        second_delta=3,
        initial_version=seed.inventory_level.version,
    )


def _listing_contract_env(
    adapter: MockCommerceAdapter,
    seed: ProductOperationsSeed,
    repository: ProductRepository,
) -> ListingContractEnv:
    """把种子行与 Mock Adapter 接到刊登契约上(方向由 Port 方法承载)。"""

    async def read_state() -> tuple[ListingStatus, int]:
        row = await repository.get_listing(seed.workspace.id, seed.listing.id)
        if row is None:
            raise LookupError("seeded listing disappeared from the platform state")
        return row.status, row.version

    async def build_change(
        target_state: ListingStatus, operation_id: str, expected_version: int
    ) -> WriteReceipt:
        command = dataclasses.replace(
            gateway_publication_command(seed.listing, operation_id=operation_id),
            expected_version=expected_version,
        )
        if target_state is ListingStatus.PUBLISHED:
            return await adapter.publish_listing(command)
        return await adapter.unpublish_listing(command)

    def build_reconcile(
        operation_id: str, expected_state: ListingStatus
    ) -> ReconcileListingCommand:
        return ReconcileListingCommand(
            operation_id=operation_id,
            workspace_id=seed.workspace.id,
            store_id=seed.store.id,
            listing_id=seed.listing.id,
            external_store_ref=str(seed.store.id),
            external_product_ref=seed.listing.external_product_ref,
            expected_state=expected_state,
        )

    return ListingContractEnv(
        gateway=adapter,
        build_change=build_change,
        build_reconcile=build_reconcile,
        read_state=read_state,
        logical_writes=adapter.count_logical_writes,
        lose_response=partial(_lose_response, adapter),
        initial_state=seed.listing.status,
        initial_version=seed.listing.version,
    )


async def test_mock_adapter_satisfies_all_supported_contracts(
    mock_adapter: MockCommerceAdapter,
    product_seeder: ProductOperationsSeeder,
    product_repository: ProductRepository,
) -> None:
    """Mock Adapter 全量满足价格 / 库存 / 刊登三套平台契约(计划 P1 Task 9)。

    三套契约共用 ``contracts.py`` 的可复用协程,平台差异只体现在环境构造里;
    每个能力用独立种子,避免上一套契约推进的版本污染下一套的写前基线。
    """
    price_seed = await product_seeder.seed()
    await assert_price_gateway_contract(
        _price_contract_env(mock_adapter, price_seed, product_repository)
    )

    inventory_seed = await product_seeder.seed()
    await assert_inventory_gateway_contract(
        _inventory_contract_env(mock_adapter, inventory_seed, product_repository)
    )

    listing_seed = await product_seeder.seed()
    await assert_listing_gateway_contract(
        _listing_contract_env(mock_adapter, listing_seed, product_repository)
    )
