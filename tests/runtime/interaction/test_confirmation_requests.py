"""Confirmation Request 与指纹测试(计划 02 Task 7 Step 1;架构文档《Runtime执行状态与恢复设计》§4.4、§6)。

第二个用例为计划文档原文;其余为 Step 3 权威语义的不变式补充:
- 指纹使用规范化 JSON 计算,绑定 Actor、Workspace、ToolCall、operation_id、
  规范化参数摘要、资源版本、Tool Schema hash、Policy 与 Execution Mode;
- verify 用当前资源版本重算指纹并比对(防 TOCTOU);
- accept/reject/cancel 为单次消费的条件更新;过期即标 EXPIRED;
- 每次创建/接受/拒绝/取消/过期都有只追加审计,payload 不含参数值。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.service import AuditService
from runtime.interaction.confirmation_service import (
    ConfirmationRequestError,
    ConfirmationService,
)
from runtime.interaction.fingerprints import (
    ConfirmationBinding,
    canonical_json,
    confirmation_fingerprint,
    normalized_arguments_digest,
)
from runtime.persistence.base import ConfirmationRequestStatus
from runtime.persistence.models.interaction import ConfirmationRequest


def _binding(**overrides: Any) -> ConfirmationBinding:
    """构造确定性测试用的绑定要素;``overrides`` 覆盖任一要素。"""
    values: dict[str, Any] = {
        "actor_id": uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e60"),
        "workspace_id": uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e61"),
        "tool_call_id": uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e62"),
        "operation_id": uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e63"),
        "normalized_arguments": {"listing_id": "L1", "price": 1},
        "resource_versions": {"listing": 1},
        "tool_schema_hash": "schema-hash-1",
        "policy_version": 3,
        "execution_mode_version": 2,
    }
    values.update(overrides)
    return ConfirmationBinding(**values)


# ---------------------------------------------------------------------------
# 计划文档原文用例
# ---------------------------------------------------------------------------


async def test_changed_resource_version_invalidates_confirmation(
    confirmation_service, accepted_confirmation
) -> None:
    result = await confirmation_service.verify(
        accepted_confirmation.id,
        current_resource_versions={"listing": 2},
    )
    assert result.valid is False
    assert result.reason_code == "CONFIRMATION_STALE"


# ---------------------------------------------------------------------------
# 指纹确定性(纯函数)
# ---------------------------------------------------------------------------


def test_confirmation_fingerprint_is_deterministic() -> None:
    """相同输入产生相同指纹。"""
    assert confirmation_fingerprint(_binding()) == confirmation_fingerprint(_binding())


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("actor_id", uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e6f")),
        ("workspace_id", uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e70")),
        ("tool_call_id", uuid.UUID("01937a6a-7b52-7d50-8f60-1a2b3c4d5e71")),
        ("operation_id", None),
        ("normalized_arguments", {"listing_id": "L1", "price": 2}),
        ("resource_versions", {"listing": 2}),
        ("tool_schema_hash", "schema-hash-2"),
        ("policy_version", 4),
        ("execution_mode_version", 3),
    ],
)
def test_confirmation_fingerprint_changes_with_any_binding_element(
    field: str, changed: Any
) -> None:
    """任一绑定要素变化都改变指纹(含 operation_id:值 → None)。"""
    base = confirmation_fingerprint(_binding())
    mutated = confirmation_fingerprint(_binding(**{field: changed}))
    assert mutated != base


def test_canonical_json_normalizes_numbers_and_key_order() -> None:
    """规范化 JSON:键排序、整数值浮点归一,键序与数值形态不影响摘要。"""
    assert canonical_json({"b": 2, "a": 1.0}) == canonical_json({"a": 1, "b": 2.0})
    assert normalized_arguments_digest({"a": 1, "b": 2.0}) == normalized_arguments_digest(
        {"b": 2.0, "a": 1}
    )
    assert normalized_arguments_digest({"price": 1}) != normalized_arguments_digest(
        {"price": 2}
    )


# ---------------------------------------------------------------------------
# verify:指纹重算比对(防 TOCTOU)
# ---------------------------------------------------------------------------


async def test_verify_with_unchanged_versions_is_valid(
    confirmation_service, accepted_confirmation
) -> None:
    result = await confirmation_service.verify(
        accepted_confirmation.id,
        current_resource_versions={"listing": 1},
    )
    assert result.valid is True
    assert result.reason_code is None


async def test_verify_detects_version_change_after_passing(
    confirmation_service, accepted_confirmation
) -> None:
    """verify 通过后资源版本再变,同一次确认的指纹不再匹配。"""
    first = await confirmation_service.verify(
        accepted_confirmation.id,
        current_resource_versions={"listing": 1},
    )
    assert first.valid is True
    second = await confirmation_service.verify(
        accepted_confirmation.id,
        current_resource_versions={"listing": 2},
    )
    assert second.valid is False
    assert second.reason_code == "CONFIRMATION_STALE"


async def test_verify_detects_tool_schema_hash_drift(
    confirmation_service: ConfirmationService, seeded_run, seeded_tool_call
) -> None:
    """Tool Schema hash 变化改变指纹;缺省 None 与 create 的 None 一致。"""
    request = await confirmation_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        tool_call_id=seeded_tool_call.id,
        normalized_arguments={"listing_id": "L1"},
        resource_versions={"listing": 1},
        tool_schema_hash="schema-hash-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await confirmation_service.accept(request.id)
    valid = await confirmation_service.verify(
        request.id,
        current_resource_versions={"listing": 1},
        tool_schema_hash="schema-hash-1",
    )
    assert valid.valid is True
    drifted = await confirmation_service.verify(
        request.id,
        current_resource_versions={"listing": 1},
        tool_schema_hash="schema-hash-2",
    )
    assert (drifted.valid, drifted.reason_code) == (False, "CONFIRMATION_STALE")
    # 失败闭合:create 绑定了 hash 而 verify 未提供当前 hash 时,视为已漂移。
    omitted = await confirmation_service.verify(
        request.id,
        current_resource_versions={"listing": 1},
    )
    assert (omitted.valid, omitted.reason_code) == (False, "CONFIRMATION_STALE")


async def test_verify_rejects_unaccepted_confirmation(
    confirmation_service: ConfirmationService, pending_confirmation
) -> None:
    result = await confirmation_service.verify(
        pending_confirmation.id,
        current_resource_versions={"listing": 1},
    )
    assert (result.valid, result.reason_code) == (False, "CONFIRMATION_NOT_ACCEPTED")


# ---------------------------------------------------------------------------
# accept/reject/cancel:单次消费与拒绝路径
# ---------------------------------------------------------------------------


async def test_accept_marks_accepted_and_writes_audit(
    audit_service: AuditService,
    seeded_workspace,
    confirmation_service: ConfirmationService,
    pending_confirmation,
) -> None:
    await confirmation_service.accept(pending_confirmation.id)
    assert (
        await pending_confirmation.reload_status() == ConfirmationRequestStatus.ACCEPTED
    )
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    accepted = [e for e in events if e.action == "CONFIRMATION_ACCEPTED"]
    assert len(accepted) == 1
    assert accepted[0].target_type == "confirmation_request"
    # 参数值不得进入审计 payload。
    assert "L1" not in json.dumps(
        [e.redacted_payload for e in events], ensure_ascii=False
    )


async def test_accept_is_single_consumption(
    confirmation_service: ConfirmationService, pending_confirmation
) -> None:
    await confirmation_service.accept(pending_confirmation.id)
    with pytest.raises(ConfirmationRequestError, match="REQUEST_NOT_PENDING"):
        await confirmation_service.accept(pending_confirmation.id)


async def test_reject_path_blocks_verify_and_writes_audit(
    audit_service: AuditService,
    seeded_workspace,
    confirmation_service: ConfirmationService,
    pending_confirmation,
) -> None:
    await confirmation_service.reject(pending_confirmation.id)
    assert (
        await pending_confirmation.reload_status() == ConfirmationRequestStatus.REJECTED
    )
    result = await confirmation_service.verify(
        pending_confirmation.id,
        current_resource_versions={"listing": 1},
    )
    assert (result.valid, result.reason_code) == (False, "CONFIRMATION_NOT_ACCEPTED")
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    assert any(e.action == "CONFIRMATION_REJECTED" for e in events)


async def test_cancel_path(
    audit_service: AuditService,
    seeded_workspace,
    confirmation_service: ConfirmationService,
    pending_confirmation,
) -> None:
    await confirmation_service.cancel(pending_confirmation.id)
    assert (
        await pending_confirmation.reload_status() == ConfirmationRequestStatus.CANCELLED
    )
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    assert any(e.action == "CONFIRMATION_CANCELLED" for e in events)


async def _expired_pending_confirmation(
    confirmation_service: ConfirmationService, seeded_run, seeded_tool_call
) -> Any:
    """已过期但仍处 PENDING 的 Confirmation Request。"""
    return await confirmation_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        tool_call_id=seeded_tool_call.id,
        normalized_arguments={"listing_id": "L1"},
        resource_versions={"listing": 1},
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )


async def test_accept_expired_confirmation_marks_expired(
    db_session: AsyncSession,
    confirmation_service: ConfirmationService,
    seeded_run,
    seeded_tool_call,
) -> None:
    """过期的 PENDING 确认在处理时被标 EXPIRED(条件更新 expires_at > now())。"""
    request = await _expired_pending_confirmation(
        confirmation_service, seeded_run, seeded_tool_call
    )
    with pytest.raises(ConfirmationRequestError, match="REQUEST_EXPIRED"):
        await confirmation_service.accept(request.id)
    fresh = (
        await db_session.scalars(
            select(ConfirmationRequest)
            .where(ConfirmationRequest.id == request.id)
            .execution_options(populate_existing=True)
        )
    ).one()
    assert fresh.status == ConfirmationRequestStatus.EXPIRED


# ---------------------------------------------------------------------------
# 过期清扫
# ---------------------------------------------------------------------------


async def test_expire_due_expires_due_confirmations_and_writes_audit(
    audit_service: AuditService,
    seeded_workspace,
    db_session: AsyncSession,
    confirmation_service: ConfirmationService,
    expire_service,
    seeded_run,
    seeded_tool_call,
) -> None:
    """批量把过期 PENDING 确认标 EXPIRED 并写审计;未过期确认不受影响。"""
    due = await _expired_pending_confirmation(
        confirmation_service, seeded_run, seeded_tool_call
    )
    # 同一 tool_call 同时只允许一个 PENDING 确认:先清扫到期的,再建未到期的。
    assert await expire_service.expire_due() == 1
    fresh = await confirmation_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        tool_call_id=seeded_tool_call.id,
        normalized_arguments={"listing_id": "L1"},
        resource_versions={"listing": 1},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert (
        await reload_confirmation_status(db_session, due.id)
        == ConfirmationRequestStatus.EXPIRED
    )
    assert (
        await reload_confirmation_status(db_session, fresh.id)
        == ConfirmationRequestStatus.PENDING
    )
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    expired_events = [e for e in events if e.action == "CONFIRMATION_EXPIRED"]
    assert len(expired_events) == 1
    assert await expire_service.expire_due() == 0


async def reload_confirmation_status(db_session: AsyncSession, request_id: Any) -> Any:
    """绕过身份映射重读确认状态(与 conftest Handle 的 reload_status 同构)。"""
    row = (
        await db_session.scalars(
            select(ConfirmationRequest)
            .where(ConfirmationRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return row.status


# ---------------------------------------------------------------------------
# 创建与过期审计:payload 无参数值
# ---------------------------------------------------------------------------


async def test_create_writes_audit_without_argument_values(
    audit_service: AuditService,
    seeded_workspace,
    confirmation_service: ConfirmationService,
    pending_confirmation,
) -> None:
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    created = [e for e in events if e.action == "CONFIRMATION_CREATED"]
    assert len(created) == 1
    payload_json = json.dumps(created[0].redacted_payload, ensure_ascii=False)
    assert "L1" not in payload_json
    assert "1.00" not in payload_json


async def test_accept_expired_writes_expiry_audit(
    audit_service: AuditService,
    seeded_workspace,
    confirmation_service: ConfirmationService,
    seeded_run,
    seeded_tool_call,
) -> None:
    request = await _expired_pending_confirmation(
        confirmation_service, seeded_run, seeded_tool_call
    )
    with pytest.raises(ConfirmationRequestError):
        await confirmation_service.accept(request.id)
    events = await audit_service.list_for_workspace(
        workspace_id=seeded_workspace.workspace.id
    )
    expired = [e for e in events if e.action == "CONFIRMATION_EXPIRED"]
    assert len(expired) == 1
