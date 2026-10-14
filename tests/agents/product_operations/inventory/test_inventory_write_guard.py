"""P1 写目标来源守卫(设计 §4.1、§5;复评 P1)。

经**真实** PreToolUse 管线证明:``adjust_inventory_quantity`` 的模型可见写目标
(``store_ref`` / ``location_ref``)未取得用户证据时,管线在 ``argument_provenance``
阶段并入诊断,``input_requirement`` 阶段返回 ``REQUIRE_INPUT``;Runtime 据此把
Run 置为 ``WAITING_INPUT`` 且**不执行** Tool——本模块用真实
:class:`AdjustInventoryQuantityHandler` + 记录型 Gateway 断言 Adapter 调用为 0。

对照面:同样的真实管线在写目标可信时不会触发守卫,继续走到写 Tool 的确认门
(``REQUIRE_CONFIRMATION``)——证明"多位置必须补充输入"与"唯一启用位置由
Service 确定性解析"并不冲突。
"""

from __future__ import annotations

import json
import uuid

from _support import InventoryEnv, RecordingInventoryGateway
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import p1_fields_for, product_registration
from agents.product_operations.inventory.models import InventoryAdjustmentReason
from agents.product_operations.inventory.tools import ADJUST_INVENTORY_QUANTITY_DEFINITION
from runtime.access.actor import ActorContext
from runtime.access.authorization import Authorizer
from runtime.capabilities.registry import ToolBinding, ToolRegistry, ToolVersionRecord
from runtime.execution.pretool.pipeline import (
    PreToolDependencies,
    PreToolUsePipeline,
    ToolCallRequest,
    default_stages,
)
from runtime.execution.pretool.profiles import (
    NoBusinessPolicy,
    PreToolProfileContext,
    PreToolProfileRegistration,
)
from runtime.execution.pretool.schema_validation import RuntimeSchemaValidator
from runtime.execution.tool_registry import RuntimeToolRegistry
from runtime.interfaces.identifiers import ToolCallId


STABLE_KEY = ADJUST_INVENTORY_QUANTITY_DEFINITION.stable_key


def _registration() -> PreToolProfileRegistration:
    """写 Tool 的 Profile 注册:只为验证来源守卫,业务策略用显式空策略。"""
    return product_registration(
        fields=p1_fields_for(STABLE_KEY),
        policy_mode="BUSINESS",
        business_policy_factory=lambda context: NoBusinessPolicy(),
    )


def _request(arguments: dict[str, object]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call_id=ToolCallId(uuid.uuid4()),
        stable_key=STABLE_KEY,
        version=ADJUST_INVENTORY_QUANTITY_DEFINITION.version,
        raw_arguments=json.dumps(arguments),
    )


def _pipeline(
    env: InventoryEnv,
    *,
    handler: object,
    answered: dict[str, str],
    request: ToolCallRequest,
) -> PreToolUsePipeline:
    definition = ADJUST_INVENTORY_QUANTITY_DEFINITION
    registry = ToolRegistry()
    record = ToolVersionRecord(
        stable_key=definition.stable_key,
        version_id=uuid.uuid4(),
        version=definition.version,
        source_kind="LOCAL",
        effect=definition.effect,
        input_schema=definition.input_json_schema,
        output_schema=definition.output_json_schema,
        schema_hash=definition.schema_hash,
        required_permissions=definition.required_permissions,
    )
    registry.register(record, make_current=True)
    registry.bind(
        record.version_id,
        ToolBinding(
            definition=definition,
            handler=handler,
            pretool_registration=_registration(),
        ),
    )
    actor: ActorContext = env.actor
    dependencies = PreToolDependencies(
        tool_registry=RuntimeToolRegistry(registry),
        schema_validator=RuntimeSchemaValidator(),
        authorizer=Authorizer(),
        profile_context=PreToolProfileContext(
            session=env.session,
            actor=actor,
            request=request,
            run_id=None,
            answered=answered,
        ),
    )
    return PreToolUsePipeline(actor=actor, stages=default_stages(dependencies))


async def _write_tool_profile_guard_fields(
    env: InventoryEnv, session: AsyncSession
) -> tuple[str, ...]:
    factory = _registration().factory
    request = _request({})
    profile = await factory.create(
        PreToolProfileContext(session=session, actor=env.actor, request=request)
    )
    return profile.provenance_guard_fields


async def test_write_tool_guards_store_ref_and_location_ref(
    inventory_env: InventoryEnv,
) -> None:
    """写 Tool 的 Profile 必须把 store_ref / location_ref 声明为守卫字段。"""
    guard_fields = await _write_tool_profile_guard_fields(
        inventory_env, inventory_env.session
    )
    assert guard_fields == ("store_ref", "sku", "location_ref")


async def test_unproven_store_ref_requires_input_without_adapter_call(
    inventory_env: InventoryEnv,
) -> None:
    """模型自行补 store_ref(无用户证据)→ WAITING_INPUT,Adapter 调用为 0。"""
    gateway = RecordingInventoryGateway()
    handler = inventory_env.make_handler(gateway)
    request = _request(
        {
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT.value,
        }
    )
    pipeline = _pipeline(inventory_env, handler=handler, answered={}, request=request)

    decision = await pipeline.evaluate(request)

    assert decision.kind == "REQUIRE_INPUT"
    assert decision.schema_diagnostic is not None
    assert decision.schema_diagnostic.field_paths == ("store_ref",)
    assert decision.input_requirement is not None
    assert decision.input_requirement.field_paths == ("store_ref",)
    # store_ref 的候选项必须是可被 store_ref 解析的店铺名称,而非内部 UUID。
    options = decision.input_requirement.options["store_ref"]
    assert [option["value"] for option in options] == ["旗舰店"]
    assert gateway.command is None


async def test_unproven_location_ref_requires_input_without_adapter_call(
    inventory_env: InventoryEnv,
) -> None:
    """多仓场景:模型自行补合法 location_ref → WAITING_INPUT,Adapter 调用为 0。"""
    gateway = RecordingInventoryGateway()
    handler = inventory_env.make_handler(gateway)
    request = _request(
        {
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "location_ref": "默认仓",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT.value,
        }
    )
    pipeline = _pipeline(
        inventory_env,
        handler=handler,
        answered={"store_ref": "旗舰店"},
        request=request,
    )

    decision = await pipeline.evaluate(request)

    assert decision.kind == "REQUIRE_INPUT"
    assert decision.schema_diagnostic is not None
    assert decision.schema_diagnostic.field_paths == ("location_ref",)
    assert gateway.command is None


async def test_trusted_targets_pass_guard_and_reach_confirmation(
    inventory_env: InventoryEnv,
) -> None:
    """store_ref 有用户确认、location_ref 缺省 → 守卫放行,走到写确认门。

    ``location_ref`` 缺省不进守卫:唯一启用位置仍由 Service 确定性解析;管线随后
    在写 Tool 的确认门停下(``REQUIRE_CONFIRMATION``),说明守卫没有把缺省的
    位置误判成"来源不可信"。
    """
    gateway = RecordingInventoryGateway()
    handler = inventory_env.make_handler(gateway)
    request = _request(
        {
            "store_ref": "旗舰店",
            "sku": "SKU-1",
            "delta": 5,
            "reason": InventoryAdjustmentReason.RECEIPT.value,
        }
    )
    pipeline = _pipeline(
        inventory_env,
        handler=handler,
        answered={"store_ref": "旗舰店"},
        request=request,
    )

    decision = await pipeline.evaluate(request)

    assert decision.kind == "REQUIRE_CONFIRMATION"
    assert gateway.command is None
