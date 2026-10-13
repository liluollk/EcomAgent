"""``update_price`` 写 Tool(计划 03 Task 6;架构文档 §4.4、§5、§7)。

模型可见输入只有 ``store_id/sku/target_price``;currency、Listing ID、
当前 Listing Version、Policy Version、operation_id 与 idempotency key 由
服务端补全,不交给模型自由生成。Handler 实现 Runtime 的
``WriteToolHandler`` 两阶段协议,只在 PreToolUse 管线 ALLOW(权限、资源范围、
价格 Policy、执行模式与确认校验全部通过)后由 ToolExecutor 调用:

1. ``prepare``:参数按确定性白名单归一化(金额 → 两位小数 ``Decimal``);
   服务端解析 ``(workspace, store, sku) → Listing``,取 currency 与当前
   ``version`` 作为乐观并发 ``expected_version``,产出服务端可信命令与可回放
   ``expected_state``(含受控 ``operation_type``)。Listing 已不存在时返回
   确定性 rejection(默认可重试性为 False:ToolCall 直接落终态,不再重排队),
   不调用平台;本阶段不产生任何外部副作用;
2. Runtime 提交 ``expected_state``(外部调用前持久化);
3. ``execute_prepared``:以 Runtime 分配的稳定 operation_id 派生幂等键,经
   注入的 :class:`UpdatePriceGateway` 执行平台写;不接受模型参数,异常交由
   ToolExecutor 归一为 UNKNOWN;
4. 返回写入前后价格、最终 Listing Version、外部引用与 Policy Version 的
   结构化回执;UNKNOWN/FAILED 只携带 reason_code,不臆测结果。

导入方向:本模块不 import adapters(方向为 adapters → agents);平台桥接
由 Worker 组合根(apps)把 :class:`UpdatePriceGateway` 实现到具体 Adapter。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.input.normalizer import normalize_arguments
from agents.product_operations.persistence.models import Listing, Product, Store
from runtime.capabilities.validation import compute_schema_hash
from runtime.execution.tool_executor import PreparedToolWrite, PreparedWriteRejection
from runtime.execution.tool_result_processor import ToolExecutionReceipt
from runtime.interfaces.tools import ToolDefinition, ToolEffect


STABLE_KEY_UPDATE_PRICE: Final = "update_price"
TOOL_VERSION: Final = 3
PRODUCT_PRICE_WRITE_PERMISSION: Final = "product.price.write"

OPERATION_TYPE: Final = "UPDATE_PRICE_LEGACY"
"""本 Tool 的受控 operation_type;恢复时据此分发到对应 Reconciler。

模型不可见、也不可填写:它由已固定的 ToolVersion 在 prepare 阶段写入。
"""

LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
INVALID_ARGUMENTS: Final = "INVALID_ARGUMENTS"


class _FrozenModel(BaseModel):
    """frozen 值模型基类;与商品域 Tool Schema 的约定一致。"""

    model_config = ConfigDict(frozen=True)


class UpdatePriceInput(_FrozenModel):
    """``update_price`` 模型可见输入:恰为 store_id/sku/target_price 三字段。"""

    store_id: str = Field(
        description=(
            "用户当前消息明确给出的店铺名称或别名;不要复制 ToolResult 中的内部 ID。"
        )
    )
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")
    target_price: Decimal = Field(
        description="最终目标售价;相对改价任务先依据读取结果完成计算再填写。"
    )


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
        "币种、版本与幂等凭据由服务端补全;写前始终校验权限与价格策略,"
        "是否需要人工确认由 Workspace 的 ExecutionMode 决定。"
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
class PreparedPriceWrite:
    """prepare 阶段解析出的可信资源与目标价。

    ``operation_id`` 不在其中:它由 Runtime 的执行边界分配,只在
    ``execute_prepared`` 中注入,模型与 Tool 参数都不能提供。
    """

    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    sku: str
    target_price: Decimal
    currency: str
    expected_version: int

    def to_command(self, operation_id: uuid.UUID) -> UpdatePriceWriteCommand:
        """注入执行边界分配的 operation_id,得到平台写命令。"""
        return UpdatePriceWriteCommand(
            operation_id=str(operation_id),
            workspace_id=self.workspace_id,
            store_id=self.store_id,
            listing_id=self.listing_id,
            external_store_ref=self.external_store_ref,
            external_product_ref=self.external_product_ref,
            sku=self.sku,
            target_price=self.target_price,
            currency=self.currency,
            expected_version=self.expected_version,
        )


@dataclass(frozen=True)
class UpdatePriceWriteReceipt:
    """平台写回执;UNKNOWN 时观测字段为 None,由调用方调度回查。

    ``retryable`` 是执行端对"这次失败是否值得重试"的显式声明(默认 False),
    原样透传给 Runtime 的回执契约,决定 ToolCall 是回 ``VALIDATING`` 还是落
    ``FAILED`` 终态。
    """

    status: str
    before_price: Decimal | None = None
    after_price: Decimal | None = None
    listing_version: int | None = None
    external_ref: str | None = None
    reason_code: str | None = None
    retryable: bool = False


class UpdatePriceGateway(Protocol):
    """平台写网关:组合根把它实现到具体 Adapter(如 MockCommerceAdapter)。"""

    async def execute(self, command: UpdatePriceWriteCommand) -> UpdatePriceWriteReceipt: ...


class UpdatePriceToolHandler:
    """``update_price`` 的外部执行端点(实现 Runtime ``WriteToolHandler`` 协议)。

    依赖注入:绑定工作区的 Session 与平台网关。``prepare`` 在短事务中读取
    Listing 与当前 version,构造服务端可信命令与可回放 ``expected_state``;
    Runtime 提交 ``expected_state`` 之后才调用 ``execute_prepared`` 写平台。
    权限、资源范围、价格 Policy 与确认校验已由 PreToolUse 管线完成;
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
        policy_version_id: uuid.UUID | None = None,
        policy_revision: int | None = None,
        policy_config_hash: str | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._session = session
        self._gateway = gateway
        self._policy_version = policy_version
        self._policy_version_id = policy_version_id
        self._policy_revision = policy_revision
        self._policy_config_hash = policy_config_hash

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        """服务端补全 → 可信命令 + ``expected_state``;不产生外部副作用。

        Listing 在 PreToolUse 之后消失(或参数无法解析)属于确定冲突:以
        ``rejection`` 表达。此时不伪造 ``expected_state``——没有可信的
        ``listing_id`` / ``currency`` / ``expected_version`` 就没有可回查的
        业务目标,只给出最小审计上下文;Runtime 据此直接落 NOT_APPLIED,
        不进入 ``EXECUTING``,更不会调用 Adapter。
        """
        arguments = normalize_arguments(dict(normalized_arguments))
        try:
            prepared_command = await self._resolve_command(arguments)
        except BaseException:
            await self._session.rollback()
            raise
        # Listing 读取事务必须在这里结束:平台 Adapter 自行开启并提交它的写入
        # 事务,不得继承组合根的读取事务。
        await self._session.commit()
        if prepared_command is None:
            rejection_context = _rejection_context(arguments, self._policy_version)
            return PreparedToolWrite(
                command=None,
                expected_state=None,
                # 拒绝同样给出确定性指纹:它随只追加的 TOOL_WRITE_REJECTED 审计
                # 留痕(last_receipt 会被后续结果覆盖,不能当不可变证据)。
                request_fingerprint=_expected_state_fingerprint(rejection_context),
                rejection=PreparedWriteRejection(
                    status="FAILED",
                    error_code=LISTING_NOT_FOUND,
                    context=rejection_context,
                ),
            )
        expected_state: dict[str, Any] = {
            "operation_type": OPERATION_TYPE,
            "listing_id": str(prepared_command.listing_id),
            "store_id": str(prepared_command.store_id),
            "sku": prepared_command.sku,
            "target_price": str(prepared_command.target_price),
            "currency": prepared_command.currency,
            "expected_version": prepared_command.expected_version,
            "policy_version": self._policy_version,
        }
        if self._policy_version_id is not None:
            expected_state["policy_version_id"] = str(self._policy_version_id)
        if self._policy_revision is not None:
            expected_state["policy_revision"] = self._policy_revision
        if self._policy_config_hash is not None:
            expected_state["policy_config_hash"] = self._policy_config_hash
        return PreparedToolWrite(
            command=prepared_command,
            expected_state=expected_state,
            request_fingerprint=_expected_state_fingerprint(expected_state),
        )

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        """只用 prepare 结果写平台;异常交由 ToolExecutor 归 UNKNOWN。

        幂等键由 ``operation_id`` 确定性派生,不接受调用方转述;模型参数不再
        参与,目标价在 prepare 阶段已固定。
        """
        del idempotency_key
        prepared_command = cast("PreparedPriceWrite", prepared.command)
        command = prepared_command.to_command(operation_id)
        receipt = await self._gateway.execute(command)
        return self._to_tool_receipt(tool_call_id, command, receipt)

    async def _resolve_command(
        self, arguments: Mapping[str, Any]
    ) -> PreparedPriceWrite | None:
        """服务端补全:归一化参数、解析 Listing,取 currency 与当前 version。"""
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
        return PreparedPriceWrite(
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
            retryable=receipt.retryable,
        )

def _format_price(price: Decimal | None) -> str | None:
    return str(price) if price is not None else None


def _expected_state_fingerprint(expected_state: Mapping[str, Any]) -> str:
    """``expected_state`` 的规范化 JSON 指纹(随 Attempt 留痕)。"""
    canonical = json.dumps(expected_state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rejection_context(
    arguments: Mapping[str, Any], policy_version: str
) -> dict[str, str]:
    """资源无法解析时的最小审计上下文。

    只记录 PreToolUse 已固定的模型输入与策略版本,不臆测 Listing 侧字段;
    它写进 ``Operation.last_receipt``,不参与 Reconciliation(拒绝不存在
    可回查的业务目标)。
    """
    return {
        "operation_type": OPERATION_TYPE,
        "store_id": str(arguments.get("store_id", "")),
        "sku": str(arguments.get("sku", "")),
        "target_price": str(arguments.get("target_price", "")),
        "policy_version": policy_version,
    }


# 说明:写 Tool 的 ToolBinding(含 execution_profile 与改价业务策略)只由 Worker
# 组合根构造——它需要 workspace 级价格配置与平台 Adapter,不能在此静态给出。
# 架构文档 §3.3 要求写 Tool 的策略/资源/执行 Profile 显式声明,因此不再提供
# 缺省绑定的模块级常量,避免出现"已注册但策略缺失"的隐性危险绑定。


__all__ = [
    "INVALID_ARGUMENTS",
    "LISTING_NOT_FOUND",
    "OPERATION_TYPE",
    "PRODUCT_PRICE_WRITE_PERMISSION",
    "STABLE_KEY_UPDATE_PRICE",
    "TOOL_VERSION",
    "UPDATE_PRICE_DEFINITION",
    "PreparedPriceWrite",
    "UpdatePriceGateway",
    "UpdatePriceInput",
    "UpdatePriceResult",
    "UpdatePriceToolHandler",
    "UpdatePriceWriteCommand",
    "UpdatePriceWriteReceipt",
]
