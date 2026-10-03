"""Input Request 事务测试(计划 02 Task 7 Step 1;架构文档《Runtime执行状态与恢复设计》§4.4、§5)。

第一个用例为计划文档原文;其余为 Step 3 权威语义的不变式补充:
- 回答只合并 ``field_paths`` 声明字段,合并结果写回挂起参数并随返回值携带
  (供 Task 9 从 Tool Resolution 重走 PreToolUse);
- 回答使用 ``WHERE status='PENDING' AND expires_at > now()`` 条件更新实现
  单次消费与过期拒绝;
- 过期即标 EXPIRED;
- 每次创建/回答/拒绝/取消/过期都有只追加审计,payload 不含回答值。
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.service import AuditService
from runtime.interaction.input_service import InputAnswerRejected, InputService
from runtime.persistence.base import InputRequestStatus
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.interaction import InputRequest


# ---------------------------------------------------------------------------
# 计划文档原文用例
# ---------------------------------------------------------------------------


async def test_input_answer_only_updates_declared_paths(input_service, pending_input) -> None:
    with pytest.raises(InputAnswerRejected, match="UNDECLARED_FIELD"):
        await input_service.answer(pending_input.id, {"store_id": "s1", "price": "1.00"})


async def test_expired_input_cannot_resume_run(input_service, expired_input) -> None:
    with pytest.raises(InputAnswerRejected, match="REQUEST_EXPIRED"):
        await input_service.answer(expired_input.id, {"store_id": "s1"})
    assert await expired_input.reload_status() == InputRequestStatus.EXPIRED


# ---------------------------------------------------------------------------
# 字段白名单:只合并声明字段并返回合并后参数
# ---------------------------------------------------------------------------


async def test_answer_merges_declared_fields_onto_pending_arguments(
    db_session: AsyncSession,
    input_service: InputService,
    seeded_run,
    seeded_tool_call: ToolCall,
) -> None:
    """声明内字段成功合并到 ToolCall 挂起参数,返回值携带合并后参数。"""
    seeded_tool_call.normalized_arguments = {"price": "2.00"}
    await db_session.flush()
    request = await input_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        field_paths=["store_id"],
        question="请补充门店 ID",
        response_schema={"type": "object"},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        tool_call_id=seeded_tool_call.id,
    )
    answer = await input_service.answer(request.id, {"store_id": "s1"})
    assert answer.merged_arguments == {"price": "2.00", "store_id": "s1"}
    assert answer.tool_call_id == seeded_tool_call.id
    assert answer.run_id == seeded_run.id
    assert answer.field_paths == ("store_id",)
    await db_session.refresh(seeded_tool_call)
    assert seeded_tool_call.normalized_arguments == {"price": "2.00", "store_id": "s1"}
    assert await reload_input_status(db_session, request.id) == InputRequestStatus.ANSWERED


async def test_answer_without_tool_call_returns_answered_values(
    input_service: InputService, pending_input
) -> None:
    """无关联 ToolCall 时,合并后参数即声明字段值本身。"""
    answer = await input_service.answer(pending_input.id, {"store_id": "s1"})
    assert answer.merged_arguments == {"store_id": "s1"}


# ---------------------------------------------------------------------------
# 单次消费与状态条件
# ---------------------------------------------------------------------------


async def test_answer_is_single_consumption(
    input_service: InputService, pending_input
) -> None:
    """同一请求只能从 PENDING 回答一次;二次回答被拒且状态不变。"""
    first = await input_service.answer(pending_input.id, {"store_id": "s1"})
    assert first.merged_arguments == {"store_id": "s1"}
    with pytest.raises(InputAnswerRejected, match="REQUEST_NOT_PENDING"):
        await input_service.answer(pending_input.id, {"store_id": "s2"})
    assert await pending_input.reload_status() == InputRequestStatus.ANSWERED


async def test_cancelled_input_cannot_be_answered(
    input_service: InputService, pending_input
) -> None:
    await input_service.cancel(pending_input.id)
    with pytest.raises(InputAnswerRejected, match="REQUEST_NOT_PENDING"):
        await input_service.answer(pending_input.id, {"store_id": "s1"})
    assert await pending_input.reload_status() == InputRequestStatus.CANCELLED


# ---------------------------------------------------------------------------
# 审计覆盖:创建/回答/拒绝/取消/过期,payload 无敏感值
# ---------------------------------------------------------------------------


async def test_create_and_answer_write_audit_without_answer_values(
    audit_service: AuditService,
    seeded_workspace,
    input_service: InputService,
    pending_input,
) -> None:
    await input_service.answer(pending_input.id, {"store_id": "s1"})
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    actions = [event.action for event in events]
    assert "INPUT_REQUEST_CREATED" in actions
    assert "INPUT_ANSWERED" in actions
    answered = next(e for e in events if e.action == "INPUT_ANSWERED")
    assert answered.target_type == "input_request"
    # 回答值不得进入审计 payload。
    assert "s1" not in json.dumps([e.redacted_payload for e in events], ensure_ascii=False)


async def test_undeclared_answer_writes_rejection_audit(
    audit_service: AuditService,
    seeded_workspace,
    input_service: InputService,
    pending_input,
) -> None:
    with pytest.raises(InputAnswerRejected):
        await input_service.answer(pending_input.id, {"store_id": "s1", "price": "1.00"})
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    rejected = [e for e in events if e.action == "INPUT_ANSWER_REJECTED"]
    assert len(rejected) == 1
    assert rejected[0].outcome == "FAILURE"
    assert rejected[0].reason_code == "UNDECLARED_FIELD"
    assert "1.00" not in json.dumps(rejected[0].redacted_payload, ensure_ascii=False)
    assert await pending_input.reload_status() == InputRequestStatus.PENDING


async def test_cancel_and_answer_expiry_write_audit(
    audit_service: AuditService,
    seeded_workspace,
    input_service: InputService,
    expired_input,
) -> None:
    with pytest.raises(InputAnswerRejected):
        await input_service.answer(expired_input.id, {"store_id": "s1"})
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    actions = [event.action for event in events]
    assert "INPUT_REQUEST_EXPIRED" in actions
    assert "INPUT_ANSWER_REJECTED" in actions
    rejected = next(e for e in events if e.action == "INPUT_ANSWER_REJECTED")
    assert rejected.reason_code == "REQUEST_EXPIRED"


# ---------------------------------------------------------------------------
# 过期清扫
# ---------------------------------------------------------------------------


async def test_expire_due_expires_due_inputs_and_writes_audit(
    audit_service: AuditService,
    seeded_workspace,
    db_session: AsyncSession,
    input_service: InputService,
    expire_service,
    seeded_run,
    expired_input,
) -> None:
    """批量把过期 PENDING 请求标 EXPIRED 并写审计;未过期请求不受影响。"""
    fresh = await input_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        field_paths=["store_id"],
        question="请补充门店 ID",
        response_schema={"type": "object"},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    expired_count = await expire_service.expire_due()
    assert expired_count == 1
    assert await expired_input.reload_status() == InputRequestStatus.EXPIRED
    assert (
        await reload_input_status(db_session, fresh.id) == InputRequestStatus.PENDING
    )
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    expired_events = [e for e in events if e.action == "INPUT_REQUEST_EXPIRED"]
    assert len(expired_events) == 1
    # 幂等:再次清扫无新增。
    assert await expire_service.expire_due() == 0


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def reload_input_status(db_session: AsyncSession, request_id) -> Any:
    """绕过身份映射重读请求状态(与 conftest Handle 的 reload_status 同构)。"""
    row = (
        await db_session.scalars(
            select(InputRequest)
            .where(InputRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return row.status
