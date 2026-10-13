"""ToolExecutor:两阶段执行编排(计划 02 Task 8;架构文档 §7、§8)。

执行序列(每步对应明确的持久边界):

1. **prepare**:注入的 :class:`WriteToolHandler` 以
   ``prepare(normalized_arguments)`` 完成服务端补全——解析可信内部资源、
   读取资源版本、执行最终业务校验——返回 :class:`PreparedToolWrite`
   (服务端命令 + 可回放 ``expected_state``);该阶段不得产生外部副作用。
   ``prepare`` 返回确定性 ``rejection`` 时**不进入执行边界**:直接落
   ``NOT_APPLIED``,不写 ``EXECUTING``、不写执行允许审计、不调用 Adapter,
   因此不存在"可能已执行"的窗口;拒绝默认不可重试(``retryable=False``),
   ToolCall 直接进终态而不再重排队;
2. **短事务 begin**::meth:`OperationService.begin` 幂等创建/读取稳定
   Operation,收敛状态机并写入 ``expected_state``(``EXECUTING`` 与 EXECUTE
   Attempt);调用方在该事务提交后才发出外部调用——崩溃后恢复可凭 Operation
   行识别"可能已执行";
3. **外部调用**:``execute_prepared(operation_id, idempotency_key, prepared)``
   只使用 prepare 阶段得到的技术命令,不得再次采用模型参数构造目标;
   handler 抛异常/超时即"响应丢失",确定性归为 UNKNOWN 回执(不重试、不
   猜测结果);
4. **PostToolUse**::class:`~runtime.execution.tool_result_processor.ToolResultProcessor`
   固定管线处理回执(协议校验、分类、Output Schema、脱敏、大小限制、投影);
5. **结果事务**::meth:`OperationService.record_result` 以 lease token +
   state_version fencing 提交状态、脱敏结果与 AgentEvent。

事务边界:begin 与 record_result 都在调用方事务内 flush,提交由编排层持有;
两段之间不持有数据库事务(架构文档 §8:外部调用期间不持有长事务)。
本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.operations import (
    BegunOperation,
    OperationOutcome,
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

REJECTION_STATUS: Final = "FAILED"
"""``PreparedWriteRejection`` 唯一允许的回执状态。"""


@dataclass(frozen=True)
class PreparedWriteRejection:
    """prepare 阶段发现的确定性冲突。

    只用于 prepare 与外部调用之间出现的、可判定的冲突(例如确认后资源已
    变化)。Runtime 据此直接落一条 ``NOT_APPLIED`` Operation:不进入
    ``EXECUTING``、不写允许审计、不调用 Adapter,因此不产生外部执行的不确定
    窗口。``status`` 只允许 ``FAILED``,由 Runtime 的分类规则落为
    ``NOT_APPLIED``。

    ``context`` 是最小审计上下文(模型输入与策略版本这类不再臆测资源侧字段的
    信息),只写进 ``last_receipt``;拒绝不存在可回查的业务目标,因此它绝不
    参与 Reconciliation。

    ``retryable`` 默认 False:确定性拒绝往往由资源状态或策略决定,重排队通常
    会再次走到同一拒绝;只有确知"再次 prepare 可能成功"时才显式置 True。
    """

    status: Literal["FAILED"]
    error_code: str
    context: Mapping[str, Any]
    payload: Mapping[str, Any] | None = None
    retryable: bool = False


@dataclass(frozen=True)
class PreparedToolWrite:
    """写 Tool 的 prepare 结果:服务端可信命令与可回放的业务预期。

    ``command`` 只在当前进程的 prepare→execute 短链路中使用;恢复只读取已
    持久化的 ``expected_state``。``request_fingerprint`` 是 ``expected_state``
    的规范化指纹,随 Attempt 与拒绝审计一起留痕。

    两态互斥,由 ``__post_init__`` 强制:
    - 可执行准备:``command`` 与 ``expected_state`` 均非空、``rejection`` 为
      None;``expected_state`` 是完整、可回查的业务目标;
    - 被拒绝准备:``rejection`` 非空、``command`` 与 ``expected_state`` 均为
      None;审计上下文由 ``rejection.context`` 承载,不伪造 ``expected_state``。

    混合状态(既给 rejection 又给命令,或两者都缺)是编排错误,必须在构造时
    失败,而不是留给下游按字段是否为 None 猜分支。
    """

    command: object | None
    expected_state: Mapping[str, Any] | None
    request_fingerprint: str
    rejection: PreparedWriteRejection | None = None

    def __post_init__(self) -> None:
        if self.rejection is not None:
            if self.command is not None or self.expected_state is not None:
                raise ValueError(
                    "rejected prepared write must not carry command or expected_state"
                )
            return
        if self.command is None or self.expected_state is None:
            raise ValueError(
                "executable prepared write requires both command and expected_state"
            )


class WriteToolHandler(Protocol):
    """写 Tool 的外部执行端点(ToolBinding 装配;测试注入 fake)。

    ``prepare`` 只做服务端补全与最终业务校验,不得产生外部副作用;
    ``execute_prepared`` 必须以 ``operation_id``/``idempotency_key`` 保证幂等
    或可回查,抛出的任何异常都被 ToolExecutor 视为"响应丢失"→ UNKNOWN。
    """

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite: ...

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt: ...


@dataclass(frozen=True)
class ExecutedToolCall:
    """一次两阶段执行的完整轨迹:登记、处理后的结果与结果事务回执。

    ``begun`` 为 None 表示本次执行没有进入执行边界(prepare 阶段确定性拒绝):
    此时没有 Operation 进入 ``EXECUTING``,也没有外部调用发生。
    """

    tool_call_id: uuid.UUID
    begun: BegunOperation | None
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
        execution_profile: ToolExecutionProfile,
        output_schema: Mapping[str, Any] | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
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
        self._output_schema = output_schema
        self._checkpoint = checkpoint
        self._execution_profile = execution_profile

    async def execute(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
    ) -> ExecutedToolCall:
        """两阶段执行:prepare → begin 并提交 → 外部调用 → record_result。

        ``prepare`` 已给出确定性 rejection 时不进入执行边界:直接落
        ``NOT_APPLIED`` 并返回(Adapter 零调用、无 ``EXECUTING``、无回查窗口)。
        """
        prepared = await self._handler.prepare(normalized_arguments=normalized_arguments)
        if prepared.rejection is not None:
            return await self._record_rejection(tool_call_id, prepared)
        # 两态互斥由 PreparedToolWrite.__post_init__ 保证;此处显式收敛类型。
        expected_target = prepared.expected_state
        assert expected_target is not None
        begun = await self._operations.begin(
            tool_call_id,
            adapter_key=self._execution_profile.adapter_key,
            expected_state=expected_target,
            supports_native_idempotency=(
                self._execution_profile.supports_native_idempotency
            ),
            supports_reconciliation=self._execution_profile.supports_reconciliation,
            request_fingerprint=prepared.request_fingerprint,
        )
        # expected_state 必须先于外部副作用提交(幂等恢复的判据来源)。
        await self._checkpoint_if_configured()
        receipt = await self._invoke_prepared_handler(
            tool_call_id=tool_call_id,
            operation_id=begun.operation_id,
            idempotency_key=begun.idempotency_key,
            prepared=prepared,
        )
        processed = await self._processor.process(
            receipt, output_schema=self._output_schema
        )
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
        await self._checkpoint_if_configured()
        return ExecutedToolCall(
            tool_call_id=tool_call_id, begun=begun, processed=processed, recorded=recorded
        )

    async def _record_rejection(
        self, tool_call_id: uuid.UUID, prepared: PreparedToolWrite
    ) -> ExecutedToolCall:
        """确定性拒绝:不 begin、不调用 Adapter,直接落 NOT_APPLIED。

        可重试性只由 ``rejection.retryable`` 决定(默认不可重试),拒绝的
        ``request_fingerprint`` 随只追加审计事件留痕:``last_receipt`` 是会被
        后续结果覆盖的最新诊断快照,不能当作不可变证据。
        """
        rejection = prepared.rejection
        assert rejection is not None
        if rejection.status != REJECTION_STATUS:
            raise ValueError("invalid prepared write rejection")
        processed = await self._processor.process(
            ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status=rejection.status,
                payload=rejection.payload,
                error_code=rejection.error_code,
                retryable=rejection.retryable,
            ),
            output_schema=self._output_schema,
        )
        if processed.operation_outcome is not OperationOutcome.NOT_APPLIED:
            raise ValueError(
                "prepared write rejection must classify as NOT_APPLIED, got "
                f"{processed.operation_outcome.value}"
            )
        recorded = await self._operations.record_rejected_preparation(
            tool_call_id,
            adapter_key=self._execution_profile.adapter_key,
            rejection_context=rejection.context,
            error_code=rejection.error_code,
            request_fingerprint=prepared.request_fingerprint,
            retryable=rejection.retryable,
            supports_native_idempotency=(
                self._execution_profile.supports_native_idempotency
            ),
            supports_reconciliation=self._execution_profile.supports_reconciliation,
        )
        await self._checkpoint_if_configured()
        return ExecutedToolCall(
            tool_call_id=tool_call_id, begun=None, processed=processed, recorded=recorded
        )

    async def _checkpoint_if_configured(self) -> None:
        if self._checkpoint is not None:
            await self._checkpoint()

    async def _invoke_prepared_handler(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        """调用 prepare 后的执行端点;异常归一化为 UNKNOWN(响应丢失注入点)。

        ``asyncio.CancelledError`` 是 BaseException,不会被捕获——取消必须穿透。
        """
        try:
            return await self._handler.execute_prepared(
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                prepared=prepared,
            )
        except Exception:  # noqa: BLE001 - 响应丢失的确定性归一(含超时)
            return ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status="UNKNOWN",
                payload=None,
                error_code=RESPONSE_LOST,
            )


__all__ = [
    "REJECTION_STATUS",
    "RESPONSE_LOST",
    "ExecutedToolCall",
    "PreparedToolWrite",
    "PreparedWriteRejection",
    "ToolExecutor",
    "WriteToolHandler",
]
