"""ToolResultProcessor 测试:固定 PostToolUse 管线(计划 02 Task 8)。

计划原文测试:
- ``test_posttool_redacts_and_filters_context_projection``:字段级脱敏与
  未声明字段过滤只作用于模型投影/前端视图;
- ``test_projection_failure_does_not_erase_applied_write``:Output Schema
  失败不清除已发生的写入(APPLIED 保持,禁止盲目重试)。
"""

import hashlib
import json
import uuid

import pytest

from runtime.execution.context_projection import (
    ProjectionLimits,
)
from runtime.execution.operations import OperationOutcome
from runtime.execution.tool_result_processor import (
    AuthoritativeResultTooLargeError,
    ToolExecutionReceipt,
    ToolResultProcessor,
)


PROCESSOR_TOOL_CALL_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def result_with_extra_field_and_secret(secret: str) -> ToolExecutionReceipt:
    """APPLIED 结果:含 schema 未声明的 extra_field 与密钥类字段。"""
    return ToolExecutionReceipt(
        tool_call_id=PROCESSOR_TOOL_CALL_ID,
        status="APPLIED",
        payload={
            "result": "ok",
            "extra_field": "internal-note",
            "api_key": secret,
        },
    )


def applied_write_with_invalid_payload() -> ToolExecutionReceipt:
    """APPLIED 结果:payload 违反 Output Schema(result 必须是 string)。"""
    return ToolExecutionReceipt(
        tool_call_id=PROCESSOR_TOOL_CALL_ID,
        status="APPLIED",
        payload={"result": 123, "unexpected": "x"},
    )


async def test_posttool_redacts_and_filters_context_projection(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """计划原文:extra_field 被过滤,secret 不出现在模型投影的任何位置。"""
    processed = await tool_result_processor.process(
        result_with_extra_field_and_secret("sk-sensitive")
    )
    assert "extra_field" not in processed.context_projection.structured_data
    assert "sk-sensitive" not in processed.context_projection.model_dump_json()
    assert processed.operation_outcome == OperationOutcome.APPLIED


async def test_projection_failure_does_not_erase_applied_write(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """计划原文:Schema 失败 → INVALID_TOOL_RESULT,但 APPLIED 保持且禁止重试。"""
    processed = await tool_result_processor.process(applied_write_with_invalid_payload())
    assert processed.operation_outcome == OperationOutcome.APPLIED
    assert processed.tool_call_error == "INVALID_TOOL_RESULT"
    assert processed.retry_allowed is False


async def test_lost_response_classifies_unknown(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """响应丢失(UNKNOWN/无法识别状态)确定性分类为 UNKNOWN,禁止重试。"""
    for status in ("UNKNOWN", "MAYBE", ""):
        processed = await tool_result_processor.process(
            ToolExecutionReceipt(tool_call_id=PROCESSOR_TOOL_CALL_ID, status=status)
        )
        assert processed.operation_outcome == OperationOutcome.UNKNOWN
        assert processed.retry_allowed is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("APPLIED", OperationOutcome.APPLIED),
        ("SUCCEEDED", OperationOutcome.APPLIED),
        ("NOT_APPLIED", OperationOutcome.NOT_APPLIED),
        ("FAILED", OperationOutcome.NOT_APPLIED),
        ("ERROR", OperationOutcome.NOT_APPLIED),
        ("TIMEOUT", OperationOutcome.UNKNOWN),
        ("WEIRD", OperationOutcome.UNKNOWN),
    ],
)
async def test_classification_is_deterministic(
    tool_result_processor: ToolResultProcessor, status: str, expected: OperationOutcome
) -> None:
    """适配器回执状态 → OperationOutcome 的映射是确定性的。"""
    processed = await tool_result_processor.process(
        ToolExecutionReceipt(tool_call_id=PROCESSOR_TOOL_CALL_ID, status=status)
    )
    assert processed.operation_outcome == expected


async def test_not_applied_allows_revalidation_only(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """NOT_APPLIED 允许重试,但只能经完整 PreToolUse 重新校验后执行。"""
    processed = await tool_result_processor.process(
        ToolExecutionReceipt(
            tool_call_id=PROCESSOR_TOOL_CALL_ID, status="NOT_APPLIED", payload={"result": "ok"}
        )
    )
    assert processed.operation_outcome == OperationOutcome.NOT_APPLIED
    assert processed.retry_allowed is True


async def test_authoritative_result_keeps_original_fields_with_secrets_redacted(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """权威结果保留原始字段(含未声明字段),仅密钥类值脱敏。"""
    processed = await tool_result_processor.process(
        result_with_extra_field_and_secret("sk-sensitive")
    )
    assert processed.authoritative_result is not None
    assert processed.authoritative_result["extra_field"] == "internal-note"
    assert processed.authoritative_result["api_key"] == "[REDACTED]"
    assert "sk-sensitive" not in json.dumps(processed.frontend_view, default=str)


async def test_truncation_marks_and_deterministic_content_hash() -> None:
    """超限数组/字符串确定性裁剪并带 truncated 标记;content_hash 可复现。"""
    processor = ToolResultProcessor(
        output_schema={
            "type": "object",
            "properties": {"items": {"type": "array"}, "note": {"type": "string"}},
        },
        limits=ProjectionLimits(max_array_items=3, max_string_length=8, max_fields=8),
    )
    payload = {
        "items": [f"item-{index}" for index in range(10)],
        "note": "x" * 100,
    }
    first = await processor.process(
        ToolExecutionReceipt(
            tool_call_id=PROCESSOR_TOOL_CALL_ID, status="APPLIED", payload=payload
        )
    )
    second = await processor.process(
        ToolExecutionReceipt(
            tool_call_id=PROCESSOR_TOOL_CALL_ID, status="APPLIED", payload=payload
        )
    )
    assert first.context_projection.truncated is True
    items = first.context_projection.structured_data["items"]
    assert len(items) == 3  # type: ignore[union-attr]
    assert first.context_projection.structured_data["note"] == "xxxxxxxx"  # type: ignore[union-attr]
    assert first.context_projection.content_hash == second.context_projection.content_hash
    expected = hashlib.sha256(
        json.dumps(
            first.context_projection.structured_data,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    assert first.context_projection.content_hash == expected


async def test_authoritative_result_over_limit_raises_deterministic_error() -> None:
    """权威结果超限(无 Artifact Store)返回确定错误,而非静默截断。"""
    processor = ToolResultProcessor(
        output_schema={"type": "object"},
        max_authoritative_bytes=64,
    )
    with pytest.raises(AuthoritativeResultTooLargeError):
        await processor.process(
            ToolExecutionReceipt(
                tool_call_id=PROCESSOR_TOOL_CALL_ID,
                status="APPLIED",
                payload={"blob": "y" * 4096},
            )
        )


async def test_non_mapping_payload_is_protocol_violation(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """协议校验:工具结果 payload 必须是 JSON 对象或空。"""
    processed = await tool_result_processor.process(
        ToolExecutionReceipt(
            tool_call_id=PROCESSOR_TOOL_CALL_ID, status="APPLIED", payload="plain-text"
        )
    )
    assert processed.tool_call_error == "INVALID_TOOL_RESULT"


async def test_unknown_result_carries_no_retry_and_no_payload(
    tool_result_processor: ToolResultProcessor,
) -> None:
    """UNKNOWN 无 payload:权威结果为空,错误码透传。"""
    processed = await tool_result_processor.process(
        ToolExecutionReceipt(
            tool_call_id=PROCESSOR_TOOL_CALL_ID,
            status="UNKNOWN",
            error_code="RESPONSE_LOST",
        )
    )
    assert processed.authoritative_result is None
    assert processed.tool_call_error == "RESPONSE_LOST"
    assert processed.retry_allowed is False
