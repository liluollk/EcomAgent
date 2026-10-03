"""ToolResultProcessor:固定 PostToolUse 管线(计划 02 Task 8;架构文档 §7)。

阶段顺序固定且不可重排:

1. **协议校验**:工具结果 payload 必须是 JSON 对象或 None;违反置
   ``tool_call_error="INVALID_TOOL_RESULT"``;
2. **Operation 结果分类**:适配器回执状态 → :class:`OperationOutcome`
   确定性分类(经 :func:`~runtime.execution.operations.classify_receipt_status`);
3. **Output Schema 校验**:jsonschema 校验 payload;失败同样置
   ``INVALID_TOOL_RESULT``,但 **APPLIED 保持**——已经发生的写入不因输出或
   投影失败被解释为未执行,也不得盲目重试(``retry_allowed=False``);
4. **字段级脱敏**:runtime.security.redaction 递归脱敏密钥类字段;权威结果
   保存原始数据(仅密钥类值脱敏),模型投影与前端视图使用完整脱敏;
5. **大小限制**:权威结果超限(无 Artifact Store)抛
   :class:`AuthoritativeResultTooLargeError` 确定错误;
6. **ContextProjection**:经 :mod:`runtime.execution.context_projection` 按
   字段/数组/字符串上限确定性裁剪,带 truncated/content_hash。

返回 :class:`ProcessedToolResult`:权威结果、模型投影与前端安全视图三者分离,
模型可分别读取。本模块为纯逻辑(不触达数据库、不写事件),状态迁移由
OperationService 在结果事务中完成。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, Final

import jsonschema
from pydantic import BaseModel, ConfigDict

from runtime.execution.context_projection import (
    ContextProjection,
    ProjectionLimits,
    build_context_projection,
)
from runtime.execution.operations import OperationOutcome, classify_receipt_status
from runtime.security.redaction import redact


INVALID_TOOL_RESULT: Final = "INVALID_TOOL_RESULT"
"""协议校验或 Output Schema 校验失败的工具结果错误码。"""

DEFAULT_MAX_AUTHORITATIVE_BYTES: Final = 262_144
"""权威结果 JSON 序列化后的最大字节数(首版无 Artifact Store)。"""


class AuthoritativeResultTooLargeError(RuntimeError):
    """权威执行结果超过大小上限;确定性错误,由编排层决定失败路径。"""

    def __init__(self, *, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"RESULT_TOO_LARGE: authoritative result is {size_bytes} bytes, "
            f"limit is {limit_bytes} bytes"
        )


class ToolExecutionReceipt(BaseModel):
    """适配器执行回执(原始输入);status 由 ToolResultProcessor 确定性分类。

    ``payload`` 类型为 ``Any``:协议校验(必须是 JSON 对象或 None)是管线的
    第一阶段,非法形状不得在模型层被拒绝,而要产生 ``INVALID_TOOL_RESULT``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: uuid.UUID
    status: str
    payload: Any = None
    external_reference: str | None = None
    error_code: str | None = None


class ProcessedToolResult(BaseModel):
    """PostToolUse 管线输出:权威结果 / 模型投影 / 前端安全视图三视图分离。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: uuid.UUID
    operation_outcome: OperationOutcome
    tool_call_error: str | None
    retry_allowed: bool
    authoritative_result: Mapping[str, Any] | None
    context_projection: ContextProjection
    frontend_view: Mapping[str, Any]


class ToolResultProcessor:
    """固定 PostToolUse 管线;注入 Output Schema 与确定性限制参数。"""

    def __init__(
        self,
        *,
        output_schema: Mapping[str, Any] | None = None,
        limits: ProjectionLimits | None = None,
        max_authoritative_bytes: int = DEFAULT_MAX_AUTHORITATIVE_BYTES,
    ) -> None:
        self._output_schema = dict(output_schema) if output_schema is not None else None
        self._limits = limits
        self._max_authoritative_bytes = max_authoritative_bytes

    async def process(self, receipt: ToolExecutionReceipt) -> ProcessedToolResult:
        """按固定顺序执行协议校验 → 分类 → Schema 校验 → 脱敏 → 限制 → 投影。"""
        # 1. 协议校验:payload 必须是 JSON 对象或 None。
        tool_call_error = receipt.error_code
        payload: Mapping[str, Any] | None = receipt.payload
        if payload is not None and not isinstance(payload, Mapping):
            tool_call_error = INVALID_TOOL_RESULT
            payload = None

        # 2. Operation 结果分类(确定性)。
        outcome = classify_receipt_status(receipt.status)

        # 3. Output Schema 校验:失败只置错误码,不改变已确定的执行结果。
        if (
            payload is not None
            and self._output_schema
            and not _validate_against_schema(payload, self._output_schema)
        ):
            tool_call_error = INVALID_TOOL_RESULT

        # 4. 字段级脱敏:权威结果保存原始数据(仅密钥类值脱敏)。
        authoritative = redact(payload) if payload is not None else None

        # 5. 大小限制:权威结果超限返回确定错误(不静默截断权威事实)。
        if authoritative is not None:
            size = len(json.dumps(authoritative, ensure_ascii=False, sort_keys=True, default=str))
            if size > self._max_authoritative_bytes:
                raise AuthoritativeResultTooLargeError(
                    size_bytes=size, limit_bytes=self._max_authoritative_bytes
                )

        # 6. 模型投影:仅 schema 声明字段 + 确定性裁剪;前端安全视图同源组装。
        allowed_fields = _declared_fields(self._output_schema)
        projection = build_context_projection(
            payload, allowed_fields=allowed_fields, limits=self._limits
        )
        frontend_view: dict[str, Any] = {
            "tool_call_id": str(receipt.tool_call_id),
            "operation_outcome": outcome.value,
            "error_code": tool_call_error,
            "external_reference": receipt.external_reference,
            "truncated": projection.truncated,
            "content_hash": projection.content_hash,
            "data": projection.structured_data,
        }
        # retry_allowed 仅对 NOT_APPLIED 为 True:允许在重新通过完整 PreToolUse
        # 后再次执行;APPLIED 无需重试,UNKNOWN 禁止盲目重试。
        retry_allowed = outcome is OperationOutcome.NOT_APPLIED
        return ProcessedToolResult(
            tool_call_id=receipt.tool_call_id,
            operation_outcome=outcome,
            tool_call_error=tool_call_error,
            retry_allowed=retry_allowed,
            authoritative_result=authoritative,
            context_projection=projection,
            frontend_view=frontend_view,
        )


def _validate_against_schema(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """jsonschema 校验;Schema 本身损坏按未通过处理(结果标记 INVALID)。"""
    try:
        jsonschema.validate(instance=dict(payload), schema=dict(schema))
    except jsonschema.exceptions.ValidationError:
        return False
    except jsonschema.exceptions.SchemaError:
        return False
    return True


def _declared_fields(schema: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Output Schema 声明的顶层字段;无 properties 时不做字段过滤。"""
    if schema is None:
        return None
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return None
    return frozenset(str(key) for key in properties)


__all__ = [
    "DEFAULT_MAX_AUTHORITATIVE_BYTES",
    "INVALID_TOOL_RESULT",
    "AuthoritativeResultTooLargeError",
    "ProcessedToolResult",
    "ToolExecutionReceipt",
    "ToolResultProcessor",
]
