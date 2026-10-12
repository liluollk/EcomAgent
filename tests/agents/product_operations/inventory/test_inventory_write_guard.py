"""P1 写目标来源守卫(设计 §4.1、§5;复评 P1)。

经**真实** PreToolUse 管线证明:``adjust_inventory_quantity`` 的模型可见写目标
(``store_ref`` / ``location_ref``)未取得用户证据时,管线在 ``argument_provenance``
阶段并入诊断,``input_requirement`` 阶段返回 ``REQUIRE_INPUT``;Runtime 据此把
Run 置为 ``WAITING_INPUT`` 且**不执行** Tool——本模块用真实
:class:`AdjustInventoryQuantityHandler` + 记录型 Gateway 断言 Adapter 调用为 0。

对照面:同样的真实管线在写目标可信时不会触发守卫,继续走到写 Tool 的确认门
(``REQUIRE_CONFIRMATION``)——证明"多位置必须补充输入"与"唯一启用位置由
Service 确定性解析"并不冲突;而目标店铺已确定却存在多个启用位置时,缺少
``location_ref`` 会在 PreToolUse 直接产生 ``REQUIRE_INPUT``。

该 ``REQUIRE_INPUT`` 是**业务输入需求**:``location_ref`` 在 Schema 中可选,
因此没有 SchemaDiagnostic,只能靠 PreToolUse 契约在无诊断时咨询业务层产生。
越权店铺相反——不生成位置候选、也不向用户追问,让 Access Control 以资源范围
确定性拒绝。
"""

from __future__ import annotations

import json
import uuid

from _support import (
    InventoryEnv,
    RecordingInventoryGateway,
    add_active_location,
    make_actor,
)
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import p1_fields_for, product_registration
from agents.product_operations.inventory.models import InventoryAdjustmentReason
from agents.product_operations.inventory.tools import ADJUST_INVENTORY_QUANTITY_DEFINITION
from runtime.access.actor import ActorContext
from runtime.access.authorization import RESOURCE_SCOPE_DENIED, Authorizer
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


class _StoreResourceResolver:
    """把写目标解析为 ``("store", str(store.id))`` 的授权资源替身。

    真实组合根用商品域的授权资源解析器;这里只需要一个确定的资源引用,用来
    验证"越权店铺不生成位置候选,而由 Access Control 确定性拒绝"。
    """

    def __init__(self, store_id: uuid.UUID) -> None:
        self._store_id = str(store_id)

    async def resolve(self, *, arguments: object) -> frozenset[tuple[str, str]]:
        del arguments
        return frozenset({("store", self._store_id)})


def _registration(
    *, resource_resolver: object | None = None
) -> PreToolProfileRegistration:
    """写 Tool 的 Profile 注册:只为验证来源守卫,业务策略用显式空策略。"""
    return product_registration(
        fields=p1_fields_for(STABLE_KEY),
        policy_mode="BUSINESS",
        business_policy_factory=lambda context: NoBusinessPolicy(),
        resource_resolver=resource_resolver,
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
    actor: ActorContext | None = None,
    resource_resolver: object | None = None,
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
            pretool_registration=_registration(resource_resolver=resource_resolver),
        ),
    )
    resolved_actor: ActorContext = env.actor if actor is None else actor
    dependencies = PreToolDependencies(
        tool_registry=RuntimeToolRegistry(registry),
        schema_validator=RuntimeSchemaValidator(),
        authorizer=Authorizer(),
        profile_context=PreToolProfileContext(
            session=env.session,
            actor=resolved_actor,
            request=request,
            run_id=None,
            answered=answered,
        ),
    )
    return PreToolUsePipeline(actor=resolved_actor, stages=default_stages(dependencies))


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


async def test_multi_location_store_without_location_ref_requires_input(
    inventory_env: InventoryEnv,
) -> None:
    """目标店铺有多个启用位置且未给出 location_ref → PreToolUse 直接 REQUIRE_INPUT。

    这才是"多位置必须补充输入":提前在 PreToolUse 产生 InputRequest,而不是让
    请求进入写 Handler 后才得到一个确定性失败;Adapter 调用同样必须为 0。
    ``location_ref`` 在 Schema 中可选,因此该需求没有 SchemaDiagnostic——它由
    "无诊断也咨询业务层"的新契约产生。
    """
    await add_active_location(
        inventory_env.session,
        inventory_env.workspace_id,
        inventory_env.store.id,
        name="二号仓",
    )
    await inventory_env.session.commit()

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

    assert decision.kind == "REQUIRE_INPUT"
    assert decision.schema_diagnostic is None
    assert decision.input_requirement is not None
    assert decision.input_requirement.field_paths == ("location_ref",)
    assert decision.detail["field_paths"] == ("location_ref",)
    assert {
        option["value"] for option in decision.input_requirement.options["location_ref"]
    } == {"默认仓", "二号仓"}
    assert gateway.command is None


async def test_out_of_scope_store_yields_no_location_candidates_and_is_denied(
    inventory_env: InventoryEnv,
) -> None:
    """越权店铺:不生成位置候选、不追问,交由 Access Control 确定性拒绝。

    位置证据的匹配集是 Workspace 全量别名表,可能指向 Actor 无权访问的店铺。
    此时既不能把该店铺的位置名称放进候选项(资源信息泄露),也不该向用户追问
    一个无权操作的目标——应继续走到 Access Control 并以 ``RESOURCE_SCOPE_DENIED``
    拒绝。
    """
    await add_active_location(
        inventory_env.session,
        inventory_env.workspace_id,
        inventory_env.store.id,
        name="二号仓",
    )
    await inventory_env.session.commit()

    gateway = RecordingInventoryGateway()
    handler = inventory_env.make_handler(gateway)
    other_actor = make_actor(
        inventory_env.workspace_id, store_ids=[uuid.uuid4()]
    )
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
        actor=other_actor,
        resource_resolver=_StoreResourceResolver(inventory_env.store.id),
    )

    decision = await pipeline.evaluate(request)

    assert decision.kind == "DENY"
    assert decision.reason_code == RESOURCE_SCOPE_DENIED
    assert decision.input_requirement is None
    assert gateway.command is None
