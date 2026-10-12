"""``get_inventory_level`` 与 ``adjust_inventory_quantity`` 测试(P1 Task 7)。

覆盖:
- Tool Contract:输入 Schema 只暴露模型可见字段,写 Tool 关闭额外字段;
- 位置解析:未给出 ``location_ref`` 时唯一启用位置自动解析、多位置要求补充;
- ``prepare``:服务端解析可信资源与版本、执行 InventoryPolicy、构造可回放
  ``expected_state``;无法解析或违反策略时返回类型化拒绝,不伪造目标;
- ``execute_prepared``:只用 prepare 结果调用 Gateway,``operation_id`` 由执行
  边界注入并从其派生幂等键;APPLIED 才有观测 payload。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from _support import (
    InventoryEnv,
    RecordingInventoryGateway,
    add_active_location,
    make_actor,
)

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.inventory.gateway import InventoryWriteReceipt
from agents.product_operations.inventory.models import (
    AdjustInventoryQuantityInput,
    AmbiguousLocationError,
    GetInventoryLevelInput,
    InventoryAdjustmentReason,
)
from agents.product_operations.inventory.service import OPERATION_TYPE
from agents.product_operations.inventory.tools import (
    ADJUST_INVENTORY_QUANTITY_DEFINITION,
    GET_INVENTORY_LEVEL_DEFINITION,
)
from agents.product_operations.persistence.models import Store
from agents.product_operations.shared.errors import (
    AMBIGUOUS_LOCATION,
    NOT_FOUND,
    ProductOperationError,
)
from agents.product_operations.shared.write_contracts import WriteStatus
from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext


def test_inventory_tools_expose_only_business_inputs() -> None:
    """输入 Schema 只含模型可见字段;写 Tool 关闭额外字段。"""
    assert set(GET_INVENTORY_LEVEL_DEFINITION.input_json_schema["properties"]) == {
        "store_ref",
        "sku",
        "location_ref",
    }
    adjust_schema = ADJUST_INVENTORY_QUANTITY_DEFINITION.input_json_schema
    assert set(adjust_schema["properties"]) == {
        "store_ref",
        "sku",
        "location_ref",
        "delta",
        "reason",
    }
    assert adjust_schema["additionalProperties"] is False
    assert set(GetInventoryLevelInput.model_fields) == {"store_ref", "sku", "location_ref"}
    assert set(AdjustInventoryQuantityInput.model_fields) == {
        "store_ref",
        "sku",
        "location_ref",
        "delta",
        "reason",
    }


async def test_missing_location_is_resolved_only_when_one_active_location(
    inventory_env: InventoryEnv,
) -> None:
    result = await inventory_env.service.get_inventory_level(
        actor=inventory_env.actor, store_ref="旗舰店", sku="SKU-1", location_ref=None
    )
    assert result.location_ref == "默认仓"
    assert result.available_quantity == 20
    assert result.inventory_version == inventory_env.level.version


async def test_multiple_locations_require_user_input(
    inventory_env: InventoryEnv,
) -> None:
    await add_active_location(
        inventory_env.session, inventory_env.workspace_id, inventory_env.store.id, name="二号仓"
    )
    await inventory_env.session.commit()

    try:
        await inventory_env.service.get_inventory_level(
            actor=inventory_env.actor, store_ref="旗舰店", sku="SKU-1", location_ref=None
        )
    except AmbiguousLocationError as error:
        assert error.code == AMBIGUOUS_LOCATION
    else:  # pragma: no cover - 期望抛错
        raise AssertionError("expected AmbiguousLocationError")


async def test_explicit_location_ref_resolves_by_name(
    inventory_env: InventoryEnv,
) -> None:
    result = await inventory_env.service.get_inventory_level(
        actor=inventory_env.actor,
        store_ref="旗舰店",
        sku="SKU-1",
        location_ref="默认仓",
    )
    assert result.location_ref == "默认仓"


async def test_unknown_location_ref_is_not_found(inventory_env: InventoryEnv) -> None:
    try:
        await inventory_env.service.get_inventory_level(
            actor=inventory_env.actor,
            store_ref="旗舰店",
            sku="SKU-1",
            location_ref="不存在的仓",
        )
    except ProductOperationError as error:
        assert error.code == NOT_FOUND
    else:  # pragma: no cover - 期望抛错
        raise AssertionError("expected NOT_FOUND")


async def test_missing_listing_is_not_found(inventory_env: InventoryEnv) -> None:
    try:
        await inventory_env.service.get_inventory_level(
            actor=inventory_env.actor, store_ref="旗舰店", sku="SKU-404", location_ref=None
        )
    except ProductOperationError as error:
        assert error.code == NOT_FOUND
    else:  # pragma: no cover - 期望抛错
        raise AssertionError("expected NOT_FOUND")


async def test_read_requires_product_read_permission(inventory_env: InventoryEnv) -> None:
    restricted = make_actor(inventory_env.workspace_id, permissions=frozenset())
    try:
        await inventory_env.service.get_inventory_level(
            actor=restricted, store_ref="旗舰店", sku="SKU-1", location_ref=None
        )
    except ToolAccessError:
        pass
    else:  # pragma: no cover - 期望拒绝
        raise AssertionError("expected ToolAccessError")


# ---------------------------------------------------------------------------
# adjust_inventory_quantity:prepare / execute_prepared
# ---------------------------------------------------------------------------


def _applied_receipt(env: InventoryEnv) -> InventoryWriteReceipt:
    return InventoryWriteReceipt(
        operation_id="unused",
        idempotency_key="unused",
        status=WriteStatus.APPLIED,
        before_quantity=env.level.available_quantity,
        after_quantity=env.level.available_quantity + 5,
        resource_version=env.level.version + 1,
        external_ref=env.location.external_location_ref,
        changed=True,
    )


async def test_prepare_builds_trusted_command_and_expected_state(
    inventory_env: InventoryEnv,
) -> None:
    gateway = RecordingInventoryGateway(_applied_receipt(inventory_env))
    handler = inventory_env.make_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "location_ref": None,
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    assert prepared.command is not None
    assert prepared.expected_state is not None
    expected_state: Mapping[str, object] = prepared.expected_state
    assert expected_state["operation_type"] == OPERATION_TYPE
    assert expected_state["location_id"] == str(inventory_env.location.id)
    assert expected_state["product_id"] == str(inventory_env.product.id)
    assert expected_state["quantity_before"] == 20
    assert expected_state["delta"] == 5
    assert expected_state["expected_quantity_after"] == 25
    assert expected_state["expected_version"] == inventory_env.level.version
    assert expected_state["reason"] == "RECEIPT"


async def test_prepare_rejects_unknown_store_ref(inventory_env: InventoryEnv) -> None:
    handler = inventory_env.make_handler(RecordingInventoryGateway())
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "不存在的店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    assert prepared.expected_state is None and prepared.command is None
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == NOT_FOUND


async def test_prepare_rejects_ambiguous_store_ref(
    inventory_env: InventoryEnv,
) -> None:
    inventory_env.session.add(
        Store(
            id=uuid.uuid4(),
            workspace_id=inventory_env.workspace_id,
            platform=StorePlatform.TAOBAO,
            external_store_ref=f"shop-{uuid.uuid4()}",
            name="旗舰店",
            aliases=[],
        )
    )
    await inventory_env.session.commit()
    handler = inventory_env.make_handler(RecordingInventoryGateway())
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "AMBIGUOUS_STORE_REFERENCE"


async def test_prepare_rejects_out_of_scope_store(inventory_env: InventoryEnv) -> None:
    # SELECTED 范围必须非空,故授一个与本店铺无关的店铺 ID。
    restricted: ActorContext = make_actor(inventory_env.workspace_id, store_ids=(uuid.uuid4(),))
    handler = inventory_env.make_handler(RecordingInventoryGateway(), actor=restricted)
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "RESOURCE_SCOPE_DENIED"


async def test_prepare_rejects_ambiguous_location(inventory_env: InventoryEnv) -> None:
    await add_active_location(
        inventory_env.session, inventory_env.workspace_id, inventory_env.store.id, name="二号仓"
    )
    await inventory_env.session.commit()
    handler = inventory_env.make_handler(RecordingInventoryGateway())
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == AMBIGUOUS_LOCATION


async def test_prepare_rejects_zero_delta(inventory_env: InventoryEnv) -> None:
    handler = inventory_env.make_handler(RecordingInventoryGateway())
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 0,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "VALIDATION_FAILED"
    assert prepared.rejection.context["rule_code"] == "ZERO_DELTA"


async def test_prepare_rejects_negative_result(inventory_env: InventoryEnv) -> None:
    handler = inventory_env.make_handler(RecordingInventoryGateway())
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": -999,
            "reason": InventoryAdjustmentReason.DAMAGE,
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "VALIDATION_FAILED"
    assert prepared.rejection.context["rule_code"] == "NEGATIVE_RESULT"


async def test_execute_prepared_uses_operation_id_and_injected_idempotency(
    inventory_env: InventoryEnv,
) -> None:
    gateway = RecordingInventoryGateway(_applied_receipt(inventory_env))
    handler = inventory_env.make_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    operation_id = uuid.uuid4()
    injected_key = f"adjust_inventory_quantity:{operation_id}"
    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=operation_id,
        idempotency_key=injected_key,
        prepared=prepared,
    )
    command = gateway.command
    assert command is not None
    assert command.operation_id == str(operation_id)
    assert command.idempotency_key == injected_key
    assert command.delta == 5
    assert command.quantity_before == 20
    assert command.expected_quantity_after == 25
    assert command.expected_version == inventory_env.level.version
    assert receipt.status == "APPLIED"
    assert receipt.payload is not None
    assert receipt.payload["available_quantity"] == 25


async def test_execute_prepared_unknown_receipt_has_no_payload(
    inventory_env: InventoryEnv,
) -> None:
    gateway = RecordingInventoryGateway(
        InventoryWriteReceipt(
            operation_id="op-unknown",
            idempotency_key="op-unknown",
            status=WriteStatus.UNKNOWN,
            reason_code="PLATFORM_OUTCOME_UNKNOWN",
        )
    )
    handler = inventory_env.make_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT,
        }
    )
    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        idempotency_key="ignored",
        prepared=prepared,
    )
    assert receipt.status == "UNKNOWN"
    assert receipt.payload is None
