"""Inventory 能力的 ToolDefinition 与绑定(P1 Task 7;设计 §4.4、§8.1)。

发布两个库存 Tool:

- ``get_inventory_level`` (v1):只读,输入为模型可见引用 ``store_ref``/``sku``/
  可选 ``location_ref``;
- ``adjust_inventory_quantity`` (v1):按增量调整库存,输入为 ``store_ref``/``sku``/
  可选 ``location_ref``/``delta``/``reason``,内部标识与幂等凭据由服务端补全。

写 Tool 的 ``ToolBinding`` 需要请求级 Actor、Session 与平台 Gateway,不能由模块级
常量给出——因此只导出 Definition,由组合根用
:func:`build_adjust_inventory_quantity_binding` 显式装配(与既有写 Tool 的约定一致)。
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.profiles import p1_fields_for, product_registration
from agents.product_operations.inventory.models import (
    AdjustInventoryQuantityInput,
    AdjustInventoryQuantityResult,
    GetInventoryLevelInput,
    InventoryLevelResult,
)
from agents.product_operations.inventory.service import (
    ADJUST_INVENTORY_QUANTITY_VERSION,
    PRODUCT_INVENTORY_WRITE_PERMISSION,
    STABLE_KEY_ADJUST_INVENTORY_QUANTITY,
    AdjustInventoryQuantityHandler,
    InventoryService,
)
from runtime.access.actor import ActorContext
from runtime.capabilities.registry import ToolBinding
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.pretool.profiles import PreToolProfileRegistration
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_GET_INVENTORY_LEVEL: Final = "get_inventory_level"
GET_INVENTORY_LEVEL_VERSION: Final = 1


def _strict_input_schema(model: type[Any], *, additional_properties: bool) -> dict[str, Any]:
    """派生 Input JSON Schema;写 Tool 关闭额外字段(未允许字段在 Schema 层拒绝)。"""
    schema: dict[str, Any] = model.model_json_schema()
    schema["additionalProperties"] = additional_properties
    return schema


GET_INVENTORY_LEVEL_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_GET_INVENTORY_LEVEL,
    version=GET_INVENTORY_LEVEL_VERSION,
    description=(
        "读取授权店铺内指定 SKU 的可用库存。店铺用名称/别名引用;位置用名称/别名"
        "引用,未给出时若该店铺仅有唯一启用位置则自动解析;存在多个位置时要求"
        "用户补充。"
    ),
    input_json_schema=GetInventoryLevelInput.model_json_schema(),
    output_json_schema=InventoryLevelResult.model_json_schema(),
    effect=ToolEffect.READ,
    required_permissions=frozenset({"product.read"}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        GetInventoryLevelInput.model_json_schema(),
        InventoryLevelResult.model_json_schema(),
    ),
)


ADJUST_INVENTORY_QUANTITY_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_ADJUST_INVENTORY_QUANTITY,
    version=ADJUST_INVENTORY_QUANTITY_VERSION,
    description=(
        "按增量调整授权店铺内指定 SKU 的库存。输入为店铺引用、SKU、可选位置引用、"
        "增量 delta 与受控调整原因;位置与商品标识、库存版本与幂等凭据由服务端"
        "补全;调整后数量不得为负;是否需要人工确认由 Workspace 的 ExecutionMode 决定。"
    ),
    input_json_schema=_strict_input_schema(
        AdjustInventoryQuantityInput, additional_properties=False
    ),
    output_json_schema=AdjustInventoryQuantityResult.model_json_schema(),
    effect=ToolEffect.WRITE,
    required_permissions=frozenset({PRODUCT_INVENTORY_WRITE_PERMISSION}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        _strict_input_schema(AdjustInventoryQuantityInput, additional_properties=False),
        AdjustInventoryQuantityResult.model_json_schema(),
    ),
)


async def get_inventory_level(
    *,
    actor: ActorContext,
    session: AsyncSession,
    store_ref: str,
    sku: str,
    location_ref: str | None = None,
) -> InventoryLevelResult:
    """经 :class:`InventoryService` 读取库存;授权与位置解析都在 Service 内。"""
    return await InventoryService(session).get_inventory_level(
        actor=actor, store_ref=store_ref, sku=sku, location_ref=location_ref
    )


GET_INVENTORY_LEVEL_BINDING: Final = ToolBinding(
    definition=GET_INVENTORY_LEVEL_DEFINITION,
    handler=get_inventory_level,
    pretool_registration=product_registration(
        fields=p1_fields_for(STABLE_KEY_GET_INVENTORY_LEVEL),
        policy_mode="NONE",
        resource_mode="WORKSPACE",
    ),
)


def build_adjust_inventory_quantity_binding(
    *,
    handler: AdjustInventoryQuantityHandler,
    pretool_registration: PreToolProfileRegistration,
    execution_profile: Any | None = None,
    write_adapter_capabilities: frozenset[str] = frozenset(),
) -> ToolBinding:
    """由组合根装配写 Tool 的绑定(Handler 与 Profile 均为请求/平台相关)。"""
    return ToolBinding(
        definition=ADJUST_INVENTORY_QUANTITY_DEFINITION,
        handler=handler,
        pretool_registration=pretool_registration,
        execution_profile=execution_profile,
        write_adapter_capabilities=write_adapter_capabilities,
    )


__all__ = [
    "ADJUST_INVENTORY_QUANTITY_DEFINITION",
    "ADJUST_INVENTORY_QUANTITY_VERSION",
    "GET_INVENTORY_LEVEL_BINDING",
    "GET_INVENTORY_LEVEL_DEFINITION",
    "GET_INVENTORY_LEVEL_VERSION",
    "STABLE_KEY_ADJUST_INVENTORY_QUANTITY",
    "STABLE_KEY_GET_INVENTORY_LEVEL",
    "build_adjust_inventory_quantity_binding",
    "get_inventory_level",
]
