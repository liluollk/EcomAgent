"""发布/取消发布 Tool 契约与写路径测试(P1 Task 8;设计 §4.5、§5.2、§6)。

覆盖:
- Tool Contract:输入 Schema 只暴露模型可见字段,写 Tool 关闭额外字段;
- ``prepare``:服务端解析可信资源与版本、执行 ListingPolicy、构造可回放
  ``expected_state``(目标发布状态 / 原因);无法解析或违反策略时返回类型化拒绝;
- ``execute_prepared``:只用 prepare 结果调用 Gateway,``operation_id`` 由执行边界
  注入并从其派生幂等键;APPLIED 才有观测 payload;
- 语义:同目标发布是幂等 no-op(``APPLIED`` + ``changed=False``、零逻辑写);
  取消发布**不**清零库存、**不**删除商品。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import cast

import pytest
from publication_support import PublicationEnv, RecordingListingGateway, make_actor

from adapters.commerce.mock_adapter import MockCommerceAdapter
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.listing.models import (
    PublishListingInput,
    UnpublishListingInput,
    UnpublishReason,
)
from agents.product_operations.listing.policy import ListingPolicyConfig
from agents.product_operations.listing.service import (
    OPERATION_TYPE,
    PreparedChangePublication,
)
from agents.product_operations.listing.tools import (
    PUBLISH_LISTING_DEFINITION,
    UNPUBLISH_LISTING_DEFINITION,
)
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.shared.errors import (
    CAPABILITY_UNSUPPORTED,
    NOT_FOUND,
    VALIDATION_FAILED,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.access.actor import ActorContext


def test_listing_publication_tools_expose_only_business_inputs() -> None:
    """输入 Schema 只含模型可见字段;写 Tool 关闭额外字段。"""
    publish_schema = PUBLISH_LISTING_DEFINITION.input_json_schema
    assert set(publish_schema["properties"]) == {"store_ref", "sku"}
    assert publish_schema["additionalProperties"] is False

    unpublish_schema = UNPUBLISH_LISTING_DEFINITION.input_json_schema
    assert set(unpublish_schema["properties"]) == {"store_ref", "sku", "reason"}
    assert unpublish_schema["additionalProperties"] is False

    assert set(PublishListingInput.model_fields) == {"store_ref", "sku"}
    assert set(UnpublishListingInput.model_fields) == {"store_ref", "sku", "reason"}


def test_publish_and_unpublish_are_writes_with_the_listing_permission() -> None:
    for definition in (PUBLISH_LISTING_DEFINITION, UNPUBLISH_LISTING_DEFINITION):
        assert definition.effect.value == "WRITE"
        assert definition.required_permissions == frozenset(
            {"product.listing.write"}
        )
    # 两个 Tool 是不同的 stable key,不能共用同一个版本记录。
    assert (
        PUBLISH_LISTING_DEFINITION.stable_key
        != UNPUBLISH_LISTING_DEFINITION.stable_key
    )


def _applied_receipt(*, changed: bool = True) -> WriteReceipt:
    return WriteReceipt(
        operation_id="unused",
        idempotency_key="unused",
        status=WriteStatus.APPLIED,
        resource_version=2,
        external_ref="item-1",
        changed=changed,
    )


async def test_prepare_builds_trusted_command_and_expected_state(
    publication_env: PublicationEnv,
) -> None:
    gateway = RecordingListingGateway(_applied_receipt())
    handler = publication_env.make_publish_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )

    assert prepared.command is not None and prepared.expected_state is not None
    expected_state: Mapping[str, object] = prepared.expected_state
    assert expected_state["operation_type"] == OPERATION_TYPE
    assert expected_state["listing_id"] == str(publication_env.listing.id)
    assert expected_state["target_state"] == "PUBLISHED"
    assert expected_state["reason"] is None
    assert expected_state["expected_version"] == publication_env.listing.version


async def test_prepare_records_controlled_reason_for_unpublish(
    publication_env: PublicationEnv,
) -> None:
    gateway = RecordingListingGateway(_applied_receipt())
    handler = publication_env.make_unpublish_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "reason": UnpublishReason.COMPLIANCE,
        }
    )

    assert prepared.command is not None and prepared.expected_state is not None
    assert prepared.expected_state["target_state"] == "UNPUBLISHED"
    assert prepared.expected_state["reason"] == "COMPLIANCE"


async def test_execute_prepared_routes_publish_to_the_publish_port_method(
    publication_env: PublicationEnv,
) -> None:
    """方向来自 Handler 子类,不是模型参数:publish 必须走 publish_listing。"""
    gateway = RecordingListingGateway(_applied_receipt())
    handler = publication_env.make_publish_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )
    operation_id = uuid.uuid4()
    injected_key = f"change_listing_publication:{operation_id}"

    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=operation_id,
        idempotency_key=injected_key,
        prepared=prepared,
    )

    assert gateway.direction == "publish"
    assert gateway.command is not None
    assert gateway.command.operation_id == str(operation_id)
    assert gateway.command.idempotency_key == injected_key
    assert gateway.command.expected_version == publication_env.listing.version
    assert receipt.status == "APPLIED"
    assert receipt.payload is not None
    assert receipt.payload["publication_state"] == "PUBLISHED"
    assert receipt.payload["changed"] is True


async def test_execute_prepared_routes_unpublish_to_the_unpublish_port_method(
    publication_env: PublicationEnv,
) -> None:
    gateway = RecordingListingGateway(_applied_receipt())
    handler = publication_env.make_unpublish_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )

    await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        idempotency_key="ignored",
        prepared=prepared,
    )

    assert gateway.direction == "unpublish"


async def test_publish_already_published_listing_is_successful_noop(
    publication_env: PublicationEnv,
) -> None:
    """种子 Listing 已是 PUBLISHED:发布是幂等 no-op,不是失败。

    把它当失败会逼模型反复重试一个已满足的请求;正确语义是 APPLIED +
    ``changed=False``,且不产生第二次逻辑写。
    """
    adapter = MockCommerceAdapter(publication_env.session)
    handler = publication_env.make_publish_handler(adapter)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )
    operation_id = uuid.uuid4()

    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=operation_id,
        idempotency_key="unused",
        prepared=prepared,
    )

    assert receipt.status == "APPLIED"
    assert receipt.payload is not None
    assert receipt.payload["publication_state"] == "PUBLISHED"
    assert receipt.payload["changed"] is False
    assert await adapter.count_logical_writes(str(operation_id)) == 0


async def test_unpublish_does_not_zero_inventory(
    publication_env: PublicationEnv,
) -> None:
    """取消发布只改发布状态:商品行与库存都不动(设计 §4.5)。"""
    adapter = MockCommerceAdapter(publication_env.session)
    handler = publication_env.make_unpublish_handler(adapter)
    scheduled_quantity = publication_env.level.available_quantity
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "reason": UnpublishReason.MANUAL,
        }
    )

    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        idempotency_key="unused",
        prepared=prepared,
    )

    assert receipt.status == "APPLIED"
    assert receipt.payload is not None
    assert receipt.payload["publication_state"] == "UNPUBLISHED"

    repository = ProductRepository(publication_env.session)
    listing = await repository.get_listing(
        publication_env.workspace_id, publication_env.listing.id
    )
    level = await repository.get_inventory_level(
        publication_env.workspace_id,
        publication_env.location.id,
        publication_env.product.id,
    )
    # Listing 仍在(没有被降级成"删除商品"),库存也没有被清零。
    assert listing is not None and listing.status is ListingStatus.UNPUBLISHED
    assert level is not None and level.available_quantity == scheduled_quantity


async def test_publish_moves_unpublished_listing_to_published(
    publication_env: PublicationEnv,
) -> None:
    """对照面:真实迁移(UNPUBLISHED → PUBLISHED)按 changed=True 落库。"""
    publication_env.listing.status = ListingStatus.UNPUBLISHED
    await publication_env.session.commit()

    adapter = MockCommerceAdapter(publication_env.session)
    handler = publication_env.make_publish_handler(adapter)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )
    assert prepared.expected_state is not None
    expected_version = prepared.expected_state["expected_version"]
    assert isinstance(expected_version, int)

    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        idempotency_key="unused",
        prepared=prepared,
    )

    assert receipt.status == "APPLIED"
    assert receipt.payload is not None
    assert receipt.payload["changed"] is True
    assert receipt.payload["listing_version"] == expected_version + 1
    listing = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, publication_env.listing.id
    )
    assert listing is not None and listing.status is ListingStatus.PUBLISHED


async def test_prepare_rejects_unknown_store_ref(publication_env: PublicationEnv) -> None:
    handler = publication_env.make_publish_handler(RecordingListingGateway())
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "不存在的店", "sku": "SKU-1"}
    )
    assert prepared.command is None and prepared.expected_state is None
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == NOT_FOUND


async def test_prepare_rejects_unknown_sku(publication_env: PublicationEnv) -> None:
    handler = publication_env.make_publish_handler(RecordingListingGateway())
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-404"}
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "LISTING_NOT_FOUND"


async def test_prepare_rejects_out_of_scope_store(publication_env: PublicationEnv) -> None:
    # SELECTED 范围必须非空,故授一个与本店铺无关的店铺 ID。
    restricted: ActorContext = make_actor(
        publication_env.workspace_id, store_ids=(uuid.uuid4(),)
    )
    handler = publication_env.make_publish_handler(
        RecordingListingGateway(), actor=restricted
    )
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "RESOURCE_SCOPE_DENIED"


async def test_prepare_rejects_unsupported_platform_capability(
    publication_env: PublicationEnv,
) -> None:
    """平台适配边界不支持刊登发布 → 确定性拒绝,不降级为删除商品/库存清零。"""
    handler = publication_env.make_publish_handler(
        RecordingListingGateway(),
        policy_config=ListingPolicyConfig(
            policy_version="listing-policy-v1", supports_publication=False
        ),
    )
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == CAPABILITY_UNSUPPORTED
    assert prepared.rejection.context["rule_code"] == CAPABILITY_UNSUPPORTED


async def test_prepare_rejects_invalid_unpublish_reason(
    publication_env: PublicationEnv,
) -> None:
    """原因不是受控枚举 → VALIDATION_FAILED + rule_code,不静默接受自由文本。"""
    handler = publication_env.make_unpublish_handler(RecordingListingGateway())
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "reason": "随便写个原因",
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == VALIDATION_FAILED
    assert prepared.rejection.context["rule_code"] == "INVALID_REASON"


async def test_prepare_rejects_non_string_store_ref(
    publication_env: PublicationEnv,
) -> None:
    handler = publication_env.make_publish_handler(RecordingListingGateway())
    prepared = await handler.prepare(normalized_arguments={"store_ref": 1, "sku": "SKU-1"})
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == VALIDATION_FAILED
    assert prepared.rejection.context["rule_code"] == "INVALID_ARGUMENTS"


async def test_execute_prepared_unknown_receipt_has_no_payload(
    publication_env: PublicationEnv,
) -> None:
    gateway = RecordingListingGateway(
        WriteReceipt(
            operation_id="op-unknown",
            idempotency_key="op-unknown",
            status=WriteStatus.UNKNOWN,
            reason_code="RESPONSE_LOST_AFTER_COMMIT",
        )
    )
    handler = publication_env.make_unpublish_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1"}
    )

    receipt = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        idempotency_key="ignored",
        prepared=prepared,
    )

    assert receipt.status == "UNKNOWN"
    assert receipt.payload is None


@pytest.mark.parametrize("reason", [UnpublishReason.MANUAL, None])
async def test_reason_is_optional_for_unpublish(
    publication_env: PublicationEnv, reason: UnpublishReason | None
) -> None:
    gateway = RecordingListingGateway(_applied_receipt())
    handler = publication_env.make_unpublish_handler(gateway)
    prepared = await handler.prepare(
        normalized_arguments={"store_ref": "旗舰店", "sku": "SKU-1", "reason": reason}
    )
    command = cast("PreparedChangePublication", prepared.command)
    expected = reason.value if reason is not None else None
    assert command.reason == expected
