"""ToolExecutor:两阶段执行编排(计划 02 Task 8;架构文档 §7、§8)。

执行序列(每步对应明确的持久边界):

1. **短事务 begin**::meth:`OperationService.begin` 幂等创建/读取稳定
   Operation(写 ``EXECUTING`` 与 EXECUTE Attempt);调用方在该事务提交后才
   发出外部调用——崩溃后恢复可凭 Operation 行识别"可能已执行";
2. **外部调用**:注入的 :class:`WriteToolHandler` 以
   ``execute(operation_id, idempotency_key, normalized_arguments)`` 执行;
   handler 抛异常/超时即"响应丢失",确定性归为 UNKNOWN 回执(不重试、不
   猜测结果);
3. **PostToolUse**::class:`~runtime.execution.tool_result_processor.ToolResultProcessor`
   固定管线处理回执(协议校验、分类、Output Schema、脱敏、大小限制、投影);
4. **结果事务**::meth:`OperationService.record_result` 以 lease token +
   state_version fencing 提交状态、脱敏结果与 AgentEvent。

事务边界:begin 与 record_result 都在调用方事务内 flush,提交由编排层持有;
两段之间不持有数据库事务(架构文档 §8:外部调用期间不持有长事务)。
本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.execution.operations import (
    BegunOperation,
    OperationResultRecord,
    OperationService,
    RecordResultOutcome,
)
from runtime.execution.tool_result_processor import (
    ProcessedToolResult,
    ToolExecutionReceipt,
    ToolResultProcessor,
)


RESPONSE_LOST: Final = "RESPONSE_LOST"
"""handler 异常/超时导致响应丢失时的确定错误码。"""


class WriteToolHandler(Protocol):
    """写 Tool 的外部执行端点(ToolBinding 装配;测试注入 fake)。

    实现方必须以 ``operation_id``/``idempotency_key`` 保证幂等或可回查;
    抛出的任何异常都被 ToolExecutor 视为"响应丢失"→ UNKNOWN。
    """

    async def execute(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        normalized_arguments: Mapping[str, Any],
    ) -> ToolExecutionReceipt: ...


@dataclass(frozen=True)
class ExecutedToolCall:
    """一次两阶段执行的完整轨迹:登记、处理后的结果与结果事务回执。"""

    tool_call_id: uuid.UUID
    begun: BegunOperation
    processed: ProcessedToolResult
    recorded: RecordResultOutcome


class ToolExecutor:
    """绑定 Run 执行边界(fencing 凭据)与注入协作对象的两阶段执行器。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        run_id: uuid.UUID,
        lease_token: uuid.UUID,
        expected_state_version: int,
        handler: WriteToolHandler,
        processor: ToolResultProcessor,
        adapter_key: str,
        supports_native_idempotency: bool = False,
        supports_reconciliation: bool = False,
    ) -> None:
        self._operations = OperationService(
            session,
            workspace_id=workspace_id,
            actor_id=actor_id,
            run_id=run_id,
            lease_token=lease_token,
            expected_state_version=expected_state_version,
        )
        self._handler = handler
        self._processor = processor
        self._adapter_key = adapter_key
        self._supports_native_idempotency = supports_native_idempotency
        self._supports_reconciliation = supports_reconciliation

    async def execute(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
    ) -> ExecutedToolCall:
        """两阶段执行:begin(短事务)→ handler(响应丢失→UNKNOWN)→ record_result。"""
        begun = await self._operations.begin(
            tool_call_id,
            adapter_key=self._adapter_key,
            supports_native_idempotency=self._supports_native_idempotency,
            supports_reconciliation=self._supports_reconciliation,
        )
        receipt = await self._invoke_handler(
            tool_call_id=tool_call_id,
            operation_id=begun.operation_id,
            idempotency_key=begun.idempotency_key,
            normalized_arguments=normalized_arguments,
        )
        processed = await self._processor.process(receipt)
        recorded = await self._operations.record_result(
            OperationResultRecord(
                tool_call_id=tool_call_id,
                outcome=processed.operation_outcome,
                payload=processed.authoritative_result,
                error_code=processed.tool_call_error,
                external_reference=receipt.external_reference,
                retry_allowed=processed.retry_allowed,
            )
        )
        return ExecutedToolCall(
            tool_call_id=tool_call_id, begun=begun, processed=processed, recorded=recorded
        )

    async def _invoke_handler(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        normalized_arguments: Mapping[str, Any],
    ) -> ToolExecutionReceipt:
        """调用 handler;任何异常/超时归一化为 UNKNOWN 回执(响应丢失注入点)。

        ``asyncio.CancelledError`` 是 BaseException,不会被捕获——取消必须穿透。
        """
        try:
            return await self._handler.execute(
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                normalized_arguments=normalized_arguments,
            )
        except Exception:  # noqa: BLE001 - 响应丢失的确定性归一(含超时)
            return ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status="UNKNOWN",
                payload=None,
                error_code=RESPONSE_LOST,
            )


__all__ = [
    "RESPONSE_LOST",
    "ExecutedToolCall",
    "ToolExecutor",
    "WriteToolHandler",
]
