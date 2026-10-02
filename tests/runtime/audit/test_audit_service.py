"""只追加审计服务测试(计划 01 Task 7 Step 1;架构文档《身份与 Workspace 权限设计》§10)。

前两个用例为计划文档原文;其余为审计不变式:
- audit_events 实际列的关联字段(actor/action/target/outcome/reason_code 与
  request/run/tool_call ID)在读取视图上可回读;
- 读取显式携带 workspace_id 过滤,不跨 Workspace;
- AuditService 协议面只含 append、读取与显式拒绝修改的 update 占位,无 delete;
- append 落库行数正确;数据库 JSONB 存脱敏后 payload,原始 Secret 不落库。
"""

import json
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import AppendAuditEvent, AuditEventView
from runtime.audit.service import AuditMutationRejected, AuditService
from runtime.persistence.models.events import AuditEvent
from runtime.persistence.models.identity import User, Workspace


# ---------------------------------------------------------------------------
# 计划文档原文用例
# ---------------------------------------------------------------------------


async def test_audit_event_cannot_be_updated(audit_service, audit_event) -> None:
    """计划原文:审计事件不可修改,任何 update 尝试都被显式拒绝。"""
    with pytest.raises(AuditMutationRejected):
        await audit_service.update(audit_event.id, {"outcome": "SUCCESS"})


async def test_audit_payload_is_redacted(audit_service) -> None:
    """计划原文:敏感 payload 在返回视图中已脱敏。"""
    event = await audit_service.append(
        action="MODEL_SECRET_UPDATED",
        payload={"api_key": "sk-sensitive"},
    )
    assert "sk-sensitive" not in json.dumps(event.redacted_payload)


# ---------------------------------------------------------------------------
# 关联字段(按 audit_events 实际列)
# ---------------------------------------------------------------------------


async def test_append_returns_all_correlation_fields(audit_service, seeded_workspace, seeded_run) -> None:
    """审计事件必含 Actor/Action/Target/Outcome/ReasonCode 与 request/run/tool_call 关联。"""
    event = await audit_service.append(
        action="CONFIRMATION_ACCEPTED",
        payload={"note": "operator approved"},
        target_type="confirmation_request",
        target_id="cr-1",
        outcome="SUCCESS",
        reason_code="OPERATOR_APPROVED",
        request_id="req-1",
        run_id=seeded_run.id,
    )
    assert event.workspace_id == seeded_workspace.workspace.id
    assert event.actor_id == seeded_workspace.user.id
    assert event.action == "CONFIRMATION_ACCEPTED"
    assert event.target_type == "confirmation_request"
    assert event.target_id == "cr-1"
    assert event.outcome == "SUCCESS"
    assert event.reason_code == "OPERATOR_APPROVED"
    assert event.request_id == "req-1"
    assert event.run_id == seeded_run.id
    assert event.tool_call_id is None
    assert event.redacted_payload == {"note": "operator approved"}
    assert event.id is not None
    assert event.created_at is not None


def test_view_exposes_optional_correlation_columns() -> None:
    """audit_events 的可选关联列(run/tool_call/request/reason)必须在读取视图上暴露。

    tool_calls 行依赖 message/tool_version 外键链,本阶段不落真实行,
    以 AppendAuditEvent 接受 tool_call_id 佐证关联可写。
    """
    assert {"reason_code", "request_id", "run_id", "tool_call_id"} <= set(
        AuditEventView.model_fields
    )
    event_input = AppendAuditEvent(
        action="TOOL_EXECUTED",
        target_type="tool_call",
        target_id="tc-1",
        outcome="SUCCESS",
        tool_call_id=uuid.uuid4(),
    )
    assert event_input.tool_call_id is not None


def test_append_input_defaults_target_to_action() -> None:
    """未显式给目标时,目标即动作本身;失败动作必须显式传 outcome。"""
    event_input = AppendAuditEvent(action="LOGIN_SUCCEEDED")
    assert event_input.target_type == "ACTION"
    assert event_input.target_id == "LOGIN_SUCCEEDED"
    assert event_input.outcome == "SUCCESS"
    failure = AppendAuditEvent(
        action="LOGIN_FAILED", outcome="FAILURE", reason_code="BAD_CREDENTIALS"
    )
    assert failure.outcome == "FAILURE"


def test_audit_models_are_frozen_and_forbid_extra_fields() -> None:
    event_input = AppendAuditEvent(action="A")
    with pytest.raises(ValidationError):
        event_input.action = "B"
    with pytest.raises(ValidationError):
        AppendAuditEvent(action="A", unknown_field="x")


# ---------------------------------------------------------------------------
# Workspace 隔离与读取
# ---------------------------------------------------------------------------


async def test_list_for_run_filters_by_workspace_and_run(audit_service, seeded_run) -> None:
    event = await audit_service.append(action="TOOL_ALLOWED", run_id=seeded_run.id)
    listed = await audit_service.list_for_run(
        workspace_id=seeded_run.workspace_id, run_id=seeded_run.id
    )
    assert [item.id for item in listed] == [event.id]
    # 换 workspace 或换 run 都取不到:读取严格按显式 (workspace_id, run_id) 过滤。
    assert await audit_service.list_for_run(
        workspace_id=uuid.uuid4(), run_id=seeded_run.id
    ) == []
    assert await audit_service.list_for_run(
        workspace_id=seeded_run.workspace_id, run_id=uuid.uuid4()
    ) == []


async def test_list_for_workspace_does_not_leak_across_workspaces(
    db_session: AsyncSession, audit_service, seeded_workspace
) -> None:
    mine = await audit_service.append(action="MEMBER_ADDED", payload={})
    other_workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    other_user_id = uuid.uuid4()
    other_user = User(id=other_user_id, login=f"user-{uuid.uuid4()}", password_hash="test-hash")
    db_session.add_all([other_workspace, other_user])
    await db_session.flush()
    other_service = AuditService(
        db_session, workspace_id=other_workspace.id, actor_id=other_user_id
    )
    theirs = await other_service.append(action="MEMBER_ADDED", payload={})

    mine_list = await audit_service.list_for_workspace(workspace_id=seeded_workspace.workspace.id)
    theirs_list = await other_service.list_for_workspace(workspace_id=other_workspace.id)
    assert [item.id for item in mine_list] == [mine.id]
    assert [item.id for item in theirs_list] == [theirs.id]
    assert mine.workspace_id == seeded_workspace.workspace.id
    assert theirs.workspace_id == other_workspace.id


async def test_get_scopes_to_explicit_workspace(audit_service, audit_event) -> None:
    fetched = await audit_service.get(
        workspace_id=audit_event.workspace_id, event_id=audit_event.id
    )
    assert fetched == audit_event
    assert (
        await audit_service.get(workspace_id=uuid.uuid4(), event_id=audit_event.id) is None
    )


# ---------------------------------------------------------------------------
# 只追加落库与脱敏落库
# ---------------------------------------------------------------------------


async def test_append_persists_exactly_the_appended_rows(
    db_session: AsyncSession, audit_service
) -> None:
    count_stmt = select(func.count()).select_from(AuditEvent)
    assert (await db_session.execute(count_stmt)).scalar_one() == 0
    await audit_service.append(action="LOGIN_SUCCEEDED")
    await audit_service.append(
        action="LOGIN_FAILED", outcome="FAILURE", reason_code="BAD_CREDENTIALS"
    )
    assert (await db_session.execute(count_stmt)).scalar_one() == 2


async def test_database_stores_redacted_payload_without_secrets(
    db_session: AsyncSession, audit_service
) -> None:
    """直接查表:JSONB 列存的是脱敏后 payload,原始 Secret 值不落库。"""
    await audit_service.append(
        action="MODEL_SECRET_UPDATED",
        payload={
            "api_key": "sk-sensitive",
            "nested": {"client_secret": "s3cret-value"},
            "items": [{"token": "tok-123"}],
            "model": "gpt-test",
        },
    )
    stored = (await db_session.execute(select(AuditEvent.redacted_payload))).scalars().all()
    assert len(stored) == 1
    blob = json.dumps(stored[0])
    assert "sk-sensitive" not in blob
    assert "s3cret-value" not in blob
    assert "tok-123" not in blob
    assert "gpt-test" in blob


# ---------------------------------------------------------------------------
# 协议面:只追加,无 delete,update 仅是显式拒绝的占位
# ---------------------------------------------------------------------------


def test_audit_service_protocol_is_append_only() -> None:
    """协议层面:AuditService 无 delete;update 仅是抛 AuditMutationRejected 的占位守卫。"""
    public_methods = {
        name
        for name, value in vars(AuditService).items()
        if callable(value) and not name.startswith("_")
    }
    assert public_methods == {"append", "get", "list_for_run", "list_for_workspace", "update"}
    assert not hasattr(AuditService, "delete")


async def test_rejected_update_leaves_stored_event_unchanged(
    db_session: AsyncSession, audit_service, audit_event
) -> None:
    """update 占位只拒绝不落库:被拒后存储行与行数都不变。"""
    with pytest.raises(AuditMutationRejected):
        await audit_service.update(audit_event.id, {"outcome": "SUCCESS"})
    stored = await audit_service.get(
        workspace_id=audit_event.workspace_id, event_id=audit_event.id
    )
    assert stored == audit_event
    count_stmt = select(func.count()).select_from(AuditEvent)
    assert (await db_session.execute(count_stmt)).scalar_one() == 1
