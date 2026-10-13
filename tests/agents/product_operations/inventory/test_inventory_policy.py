"""InventoryPolicy 单元测试(P1 Task 7;设计 §5.2、§6)。

规则覆盖:原因受控枚举、``delta != 0``、位置属于店铺、(位置, 商品) 库存存在、
调整后数量非负、``expected_version`` 一致。任何失败都返回确定性 ``reason_code``
的 denied 决策,不抛异常。
"""

from __future__ import annotations

import uuid

from _support import INVENTORY_POLICY_CONFIG, InventoryEnv, add_active_location
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.inventory.models import InventoryAdjustmentReason
from agents.product_operations.inventory.policy import (
    InventoryPolicy,
    InventoryPolicyDecision,
)
from agents.product_operations.persistence.models import Store


async def _evaluate(
    env: InventoryEnv,
    *,
    delta: int,
    reason: str = InventoryAdjustmentReason.RECEIPT.value,
    expected_version: int | None = None,
    location_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
) -> InventoryPolicyDecision:
    policy = InventoryPolicy(env.session, INVENTORY_POLICY_CONFIG)
    return await policy.evaluate(
        workspace_id=env.workspace_id,
        store_id=store_id or env.store.id,
        location_id=location_id or env.location.id,
        product_id=env.product.id,
        delta=delta,
        reason=reason,
        expected_version=env.level.version if expected_version is None else expected_version,
    )


async def test_policy_allows_valid_adjustment(inventory_env: InventoryEnv) -> None:
    decision = await _evaluate(inventory_env, delta=5)
    assert decision.allowed is True
    assert decision.policy_version == "inventory-policy-v1"
    assert decision.inventory_version == inventory_env.level.version


async def test_policy_rejects_zero_delta(inventory_env: InventoryEnv) -> None:
    decision = await _evaluate(inventory_env, delta=0)
    assert decision.allowed is False
    assert decision.reason_code == "ZERO_DELTA"


async def test_policy_rejects_negative_result(inventory_env: InventoryEnv) -> None:
    decision = await _evaluate(inventory_env, delta=-100)
    assert decision.allowed is False
    assert decision.reason_code == "NEGATIVE_RESULT"


async def test_policy_rejects_version_conflict(inventory_env: InventoryEnv) -> None:
    decision = await _evaluate(
        inventory_env, delta=5, expected_version=inventory_env.level.version + 1
    )
    assert decision.allowed is False
    assert decision.reason_code == "INVENTORY_VERSION_CONFLICT"


async def test_policy_rejects_unknown_reason(inventory_env: InventoryEnv) -> None:
    decision = await _evaluate(inventory_env, delta=5, reason="FREE_TEXT")
    assert decision.allowed is False
    assert decision.reason_code == "INVALID_REASON"


async def test_policy_rejects_location_not_in_store(
    inventory_env: InventoryEnv, db_session: AsyncSession
) -> None:
    other_store = Store(
        id=uuid.uuid4(),
        workspace_id=inventory_env.workspace_id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"shop-{uuid.uuid4()}",
        name="另一家店",
        aliases=[],
    )
    db_session.add(other_store)
    await db_session.flush()
    other_location = await add_active_location(
        db_session, inventory_env.workspace_id, other_store.id, name="别家仓"
    )
    await db_session.commit()

    decision = await _evaluate(inventory_env, delta=5, location_id=other_location.id)
    assert decision.allowed is False
    assert decision.reason_code == "LOCATION_NOT_IN_STORE"
