"""商品模块公共写契约与错误模型测试(P1 Task 3)。

不变式:
- 写回执的 ``UNKNOWN`` 不得携带任何观测到的状态(否则回查会拿到伪事实);
- 规范错误码是跨模块共享的稳定字符串,不随实现语言或文案漂移;
- 商品 Operation 的 ``operation_type`` 与 Reconciler 的注册一一对应。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from agents.product_operations.shared.errors import (
    AMBIGUOUS_LOCATION,
    AUTH_EXPIRED,
    CAPABILITY_UNSUPPORTED,
    INVALID_STATE_TRANSITION,
    NOT_FOUND,
    OUTCOME_UNKNOWN,
    PLATFORM_UNAVAILABLE,
    RATE_LIMITED,
    VALIDATION_FAILED,
    VERSION_CONFLICT,
    ProductOperationError,
)
from agents.product_operations.shared.reconciliation import (
    OperationReconcilerRegistry,
    ProductOperationReconciler,
    resolve_persisted_workspace_id,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.execution.reconciliation import ReconcileResult


OPERATION_TYPE = "SET_LISTING_PRICE"


class _FixedReconciler:
    """确定性回查替身:按要求结论回应。"""

    def __init__(self, result: ReconcileResult) -> None:
        self._result = result
        self.seen: list[Mapping[str, Any]] = []

    async def reconcile(self, expected_state: Mapping[str, Any]) -> ReconcileResult:
        self.seen.append(dict(expected_state))
        return self._result


# ---- 写回执 ----
def test_unknown_receipt_cannot_claim_observed_state() -> None:
    with pytest.raises(ValueError, match="UNKNOWN receipt"):
        WriteReceipt(
            operation_id="op-1",
            idempotency_key="idem-1",
            status=WriteStatus.UNKNOWN,
            resource_version=2,
        )


def test_write_status_covers_all_four_outcomes() -> None:
    assert {status.value for status in WriteStatus} == {
        "APPLIED",
        "NOT_APPLIED",
        "UNKNOWN",
        "FAILED",
    }


def test_applied_receipt_may_carry_observed_state() -> None:
    receipt = WriteReceipt(
        operation_id="op-1",
        idempotency_key="idem-1",
        status=WriteStatus.APPLIED,
        resource_version=3,
        external_ref="ext-3",
        changed=True,
    )
    assert receipt.resource_version == 3
    assert receipt.changed is True


# ---- 规范错误 ----
def test_error_codes_are_stable_strings() -> None:
    assert (
        AUTH_EXPIRED,
        RATE_LIMITED,
        CAPABILITY_UNSUPPORTED,
        NOT_FOUND,
        VALIDATION_FAILED,
        VERSION_CONFLICT,
        PLATFORM_UNAVAILABLE,
        OUTCOME_UNKNOWN,
        AMBIGUOUS_LOCATION,
        INVALID_STATE_TRANSITION,
    ) == (
        "AUTH_EXPIRED",
        "RATE_LIMITED",
        "CAPABILITY_UNSUPPORTED",
        "NOT_FOUND",
        "VALIDATION_FAILED",
        "VERSION_CONFLICT",
        "PLATFORM_UNAVAILABLE",
        "OUTCOME_UNKNOWN",
        "AMBIGUOUS_LOCATION",
        "INVALID_STATE_TRANSITION",
    )


def test_product_operation_error_carries_code_and_detail() -> None:
    error = ProductOperationError(VERSION_CONFLICT, "resource version changed")
    assert error.code == VERSION_CONFLICT
    assert VERSION_CONFLICT in str(error)


# ---- Reconciler Registry ----
def test_reconciler_registry_rejects_duplicate_registration() -> None:
    registry = OperationReconcilerRegistry()
    reconciler = _FixedReconciler(ReconcileResult.APPLIED)
    registry.register(OPERATION_TYPE, reconciler)
    with pytest.raises(ValueError, match="duplicate reconciler"):
        registry.register(OPERATION_TYPE, reconciler)


def test_reconciler_registry_requires_registered_type() -> None:
    registry = OperationReconcilerRegistry()
    with pytest.raises(ValueError, match="unregistered operation type"):
        registry.require(OPERATION_TYPE)


async def test_reconciler_registry_dispatches_by_operation_type() -> None:
    registry = OperationReconcilerRegistry()
    reconciler: ProductOperationReconciler = _FixedReconciler(ReconcileResult.NOT_APPLIED)
    registry.register(OPERATION_TYPE, reconciler)

    resolved = registry.require(OPERATION_TYPE)
    assert resolved is reconciler
    assert await resolved.reconcile({"operation_type": OPERATION_TYPE}) is (
        ReconcileResult.NOT_APPLIED
    )


# ---- 持久化 Workspace 绑定 ----
def test_resolve_persisted_workspace_id_accepts_matching_workspace() -> None:
    workspace_id = uuid.uuid4()
    resolved = resolve_persisted_workspace_id(
        {"workspace_id": str(workspace_id)}, current_workspace_id=workspace_id
    )
    assert resolved == workspace_id


def test_resolve_persisted_workspace_id_rejects_mismatch() -> None:
    """持久化 Workspace 与当前 Workspace 不一致 → fail closed(不得改用当前值)。"""
    with pytest.raises(ProductOperationError) as exc:
        resolve_persisted_workspace_id(
            {"workspace_id": str(uuid.uuid4())}, current_workspace_id=uuid.uuid4()
        )
    assert exc.value.code == VALIDATION_FAILED


def test_resolve_persisted_workspace_id_rejects_missing() -> None:
    with pytest.raises(ProductOperationError) as exc:
        resolve_persisted_workspace_id({}, current_workspace_id=uuid.uuid4())
    assert exc.value.code == VALIDATION_FAILED


def test_resolve_persisted_workspace_id_rejects_unparsable() -> None:
    with pytest.raises(ProductOperationError) as exc:
        resolve_persisted_workspace_id(
            {"workspace_id": "not-a-uuid"}, current_workspace_id=uuid.uuid4()
        )
    assert exc.value.code == VALIDATION_FAILED
