"""``update_price`` 写 Tool(计划 03 Task 6;架构文档 §4.4、§5、§7)。

模型可见输入只有 ``store_id/sku/target_price``;currency、Listing ID、
当前 Listing Version、Policy Version、operation_id 与 idempotency key 由
服务端补全,不交给模型自由生成。Handler 实现 Runtime 的
``WriteToolHandler`` 协议,只在 PreToolUse 管线 ALLOW(权限、资源范围、
价格 Policy、执行模式与确认校验全部通过)后由 ToolExecutor 以两阶段语义
调用:

1. 参数按确定性白名单归一化(金额 → 两位小数 ``Decimal``);
2. 服务端解析 ``(workspace, store, sku) → Listing``,取 currency 与当前
   ``version`` 作为乐观并发 ``expected_version``;Listing 不存在返回确定
   FAILED,不做外部调用;
3. 以 Runtime 分配的稳定 operation_id(首次进入确认前分配,重复提交不变)
   派生幂等键,经注入的 :class:`UpdatePriceGateway` 执行平台写;Listing
   读取事务在 Gateway 请求前提交,平台 Adapter 自己负责写入协议;
4. 返回写入前后价格、最终 Listing Version、外部引用与 Policy Version 的
   结构化回执;UNKNOWN/FAILED 只携带 reason_code,不臆测结果。

导入方向:本模块不 import adapters(方向为 adapters → agents);平台桥接
由 Worker 组合根(apps)把 :class:`UpdatePriceGateway` 实现到具体 Adapter。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.normalizer import normalize_arguments
from agents.product_operations.persistence.models import Listing, Product, Store
from runtime.capabilities.registry import ToolBinding
from runtime.capabilities.validation import (
    OPERATION_CAPABILITY,
    RECONCILIATION_CAPABILITY,
    compute_schema_hash,
)
from runtime.execution.tool_result_processor import ToolExecutionReceipt
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_UPDATE_PRICE: Final = "update_price"
TOOL_VERSION: Final = 1
PRODUCT_PRICE_WRITE_PERMISSION: Final = "product.price.write"

LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
INVALID_ARGUMENTS: Final = "INVALID_ARGUMENTS"

_WRITE_ADAPTER_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {OPERATION_CAPABILITY, RECONCILIATION_CAPABILITY}
)


class _FrozenModel(BaseModel):
    """frozen 值模型基类;与商品域 Tool Schema 的约定一致。"""

    model_config = ConfigDict(frozen=True)


class UpdatePriceInput(_FrozenModel):
    """``update_price`` 模型可见输入:恰为 store_id/sku/target_price 三字段。"""

    store_id: str
    sku: str
    target_price: Decimal


class UpdatePriceResult(_FrozenModel):
    """``update_price`` 输出回执(架构文档 §4.4 白名单)。

    价格以字符串表达(与渠道两位小数金额一致);UNKNOWN 时无观测 payload。
    """

    operation_id: str
    policy_version: str
    store_id: str
    sku: str
    currency: str | None = None
    before_price: str | None = None
    after_price: str | None = None
    listing_version: int | None = None
    external_ref: str | None = None


def _input_schema() -> dict[str, Any]:
    """Input JSON Schema:模型可见字段白名单,未允许字段在 Schema 层拒绝。"""
    schema = UpdatePriceInput.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _output_schema() -> dict[str, Any]:
    return UpdatePriceResult.model_json_schema()


UPDATE_PRICE_DEFINITION: Final = ToolDefinition(
    stable_key=STABLE_KEY_UPDATE_PRICE,
    version=TOOL_VERSION,
    description=(
        "在授权店铺内修改指定 SKU 的售价。输入为店铺、SKU 与目标价;"
        "币种、版本与幂等凭据由服务端补全,写前经权限、价格策略与用户确认。"
    ),
    input_json_schema=_input_schema(),
    output_json_schema=_output_schema(),
    effect=ToolEffect.WRITE,
    required_permissions=frozenset({PRODUCT_PRICE_WRITE_PERMISSION}),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(_input_schema(), _output_schema()),
)


# ---- 服务端补全后的写命令与平台回执(与平台桥接的边界值对象) ----


@dataclass(frozen=True)
class UpdatePriceWriteCommand:
    """服务端补全后的写命令;模型不可见字段全部就位后才发往平台。"""

    operation_id: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    sku: str
    target_price: Decimal
    currency: str
    expected_version: int

    @property
    def idempotency_key(self) -> str:
        """幂等键:Runtime 分配的稳定 operation_id 的确定性派生。"""
        return f"update_price:{self.operation_id}"


@dataclass(frozen=True)
class UpdatePriceWriteReceipt:
    """平台写回执;UNKNOWN 时观测字段为 None,由调用方调度回查。"""

    status: str
    before_price: Decimal | None = None
    after_price: Decimal | None = None
    listing_version: int | None = None
    external_ref: str | None = None
    reason_code: str | None = None


class UpdatePriceGateway(Protocol):
    """平台写网关:组合根把它实现到具体 Adapter(如 MockCommerceAdapter)。"""

    async def execute(self, command: UpdatePriceWriteCommand) -> UpdatePriceWriteReceipt: ...


class UpdatePriceToolHandler:
    """``update_price`` 的外部执行端点(实现 Runtime ``WriteToolHandler`` 协议)。

    依赖注入:绑定工作区的 Session 与平台网关。Listing 查询在短事务中
    生成带 expected_version 的命令,提交后才调用 Gateway。权限、资源范围、价格
    Policy 与确认校验已由 PreToolUse 管线完成,此处不再重复业务校验;
    ``operation_id`` 与幂等键由 Runtime 的执行边界分配/派生,本 Handler
    不生成、不接受模型转述。
    """

    def __init__(
        self,
        *,
        workspace_id: uuid.UUID,
        session: AsyncSession,
        gateway: UpdatePriceGateway,
        policy_version: str,
    ) -> None:
        self._workspace_id = workspace_id
        self._session = session
        self._gateway = gateway
        self._policy_version = policy_version

    async def execute(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        normalized_arguments: Mapping[str, Any],
    ) -> ToolExecutionReceipt:
        """服务端补全 → 平台写 → 结构化回执;异常交由 ToolExecutor 归 UNKNOWN。"""
        del idempotency_key  # 幂等键由 operation_id 确定性派生,不接受调用方转述。
        try:
            command = await self._build_command(operation_id, normalized_arguments)
            # Listing 读取和版本快照先提交;外部 Gateway 不得继承该事务。
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            raise
        if command is None:
            # 参数/Listing 无法解析:确定失败,零外部调用、零状态变更。
            return self._failed(tool_call_id, LISTING_NOT_FOUND)
        receipt = await self._gateway.execute(command)
        return self._to_tool_receipt(tool_call_id, command, receipt)

    async def _build_command(
        self, operation_id: uuid.UUID, raw_arguments: Mapping[str, Any]
    ) -> UpdatePriceWriteCommand | None:
        """服务端补全:归一化参数、解析 Listing,取 currency 与当前 version。"""
        arguments = normalize_arguments(dict(raw_arguments))
        store_id = arguments.get("store_id")
        sku = arguments.get("sku")
        target_price = arguments.get("target_price")
        if not isinstance(store_id, str) or not isinstance(sku, str):
            return None
        if not isinstance(target_price, Decimal):
            return None
        try:
            parsed_store_id = uuid.UUID(store_id)
        except ValueError:
            return None
        listing_and_store = await self._resolve_listing(parsed_store_id, sku)
        if listing_and_store is None:
            return None
        listing, store = listing_and_store
        return UpdatePriceWriteCommand(
            operation_id=str(operation_id),
            workspace_id=self._workspace_id,
            store_id=parsed_store_id,
            listing_id=listing.id,
            external_store_ref=store.external_store_ref,
            external_product_ref=listing.external_product_ref,
            sku=sku,
            target_price=target_price,
            currency=listing.currency,
            expected_version=listing.version,
        )

    async def _resolve_listing(
        self, store_id: uuid.UUID, sku: str
    ) -> tuple[Listing, Store] | None:
        """按 (workspace, store, sku) 解析 Listing;populate_existing 防旧版本。"""
        stmt = (
            select(Listing, Store)
            .join(Product, Listing.product_id == Product.id)
            .join(Store, Listing.store_id == Store.id)
            .where(
                Listing.workspace_id == self._workspace_id,
                Listing.store_id == store_id,
                Product.sku == sku,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).one_or_none()

    def _to_tool_receipt(
        self,
        tool_call_id: uuid.UUID,
        command: UpdatePriceWriteCommand,
        receipt: UpdatePriceWriteReceipt,
    ) -> ToolExecutionReceipt:
        """平台回执 → Runtime ToolExecutionReceipt(payload 对齐 Output Schema)。"""
        payload: dict[str, Any] | None = None
        if receipt.status == "APPLIED":
            result = UpdatePriceResult(
                operation_id=command.operation_id,
                policy_version=self._policy_version,
                store_id=str(command.store_id),
                sku=command.sku,
                currency=command.currency,
                before_price=_format_price(receipt.before_price),
                after_price=_format_price(receipt.after_price),
                listing_version=receipt.listing_version,
                external_ref=receipt.external_ref,
            )
            payload = result.model_dump(mode="json", exclude_none=True)
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            status=receipt.status,
            payload=payload,
            external_reference=receipt.external_ref,
            error_code=receipt.reason_code,
        )

    @staticmethod
    def _failed(tool_call_id: uuid.UUID, reason_code: str) -> ToolExecutionReceipt:
        """确定性失败回执:FAILED 由 Runtime 分类为 NOT_APPLIED(允许重走校验)。"""
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            status="FAILED",
            payload=None,
            external_reference=None,
            error_code=reason_code,
        )


def _format_price(price: Decimal | None) -> str | None:
    return str(price) if price is not None else None


UPDATE_PRICE_BINDING: Final = ToolBinding(
    definition=UPDATE_PRICE_DEFINITION,
    handler=None,  # 组合根注入绑定 workspace 与平台的 UpdatePriceToolHandler 实例。
    write_adapter_capabilities=_WRITE_ADAPTER_CAPABILITIES,
)


__all__ = [
    "INVALID_ARGUMENTS",
    "LISTING_NOT_FOUND",
    "PRODUCT_PRICE_WRITE_PERMISSION",
    "STABLE_KEY_UPDATE_PRICE",
    "TOOL_VERSION",
    "UPDATE_PRICE_BINDING",
    "UPDATE_PRICE_DEFINITION",
    "UpdatePriceGateway",
    "UpdatePriceInput",
    "UpdatePriceResult",
    "UpdatePriceToolHandler",
    "UpdatePriceWriteCommand",
    "UpdatePriceWriteReceipt",
]
