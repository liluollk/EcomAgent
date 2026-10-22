"""价格回查与写入预算验收测试(P1 Task 6 与 Task 6 前置;设计 §5.3、§6)。

覆盖:
- ``build_reconcile_price_command`` 只从 ``Operation.expected_state`` 构造命令,
  缺字段/错 ``operation_type`` fail closed;
- :class:`PriceReconciler` 委托注入的 Gateway,目标价只来自持久化预期;
- **Task 6 前置验收**:通用 Runtime 的写入重试预算按 ``OperationAttempt``
  (``kind=EXECUTE``)裁决 ``NOT_APPLIED``——预算未耗尽回 ``VALIDATING``,耗尽则
  ToolCall 落 ``FAILED`` 终态且不再重排,``direct_execute`` 恒为 False,Adapter
  逻辑写次数不超过预算。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.commerce.mock_adapter import MockCommerceAdapter
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    PriceSnapshot,
    PriceWriteReceipt,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.pricing.reconciliation import (
    PriceReconciler,
    build_reconcile_price_command,
)
from agents.product_operations.pricing.service import OPERATION_TYPE, PreparedSetPrice
from agents.product_operations.shared.errors import VALIDATION_FAILED, ProductOperationError
from runtime.execution.reconciliation import (
    ReconcileResult,
    ReconciliationService,
)
from runtime.execution.run_service import RunService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    ConfigScope,
    MembershipRole,
    MessageRole,
    MessageStatus,
    ModelBackendType,
    OperationAttemptKind,
    OperationAttemptStatus,
    RunNextStep,
    ToolCallStatus,
    ToolEffect,
)
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
    ToolDefinition,
    ToolVersion,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import Message
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


def _expected_state() -> dict[str, Any]:
    """由 ``PreparedSetPrice`` 产出的规范化业务预期(prepare 的真实形状)。"""
    prepared = PreparedSetPrice(
        workspace_id=uuid.uuid4(),
        store_ref="淘宝旗舰店",
        store_id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        external_store_ref="shop-1",
        external_product_ref="item-1",
        sku="SKU-001",
        target_price=Decimal("100.00"),
        currency="CNY",
        expected_version=3,
        policy_version="price-policy-v1",
    )
    return prepared.expected_state()


class _FixedGateway:
    """返回固定回查结论的测试 Gateway。"""

    def __init__(self, result: ReconcileResult) -> None:
        self.command: ReconcilePriceCommand | None = None
        self._result = result

    async def get_price(self, query: PriceQuery) -> PriceSnapshot:  # pragma: no cover
        raise NotImplementedError

    async def set_price(  # pragma: no cover
        self, command: SetListingPriceCommand
    ) -> PriceWriteReceipt:
        raise NotImplementedError

    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        self.command = command
        return self._result


def test_build_reconcile_price_command_uses_expected_state() -> None:
    expected_state = _expected_state()
    command = build_reconcile_price_command(
        operation_id="op-1",
        expected_state=expected_state,
        workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
    )
    assert command.operation_id == "op-1"
    assert command.expected_price == Decimal("100.00")
    assert command.currency == "CNY"
    assert command.listing_id == uuid.UUID(str(expected_state["listing_id"]))
    assert command.external_store_ref == "shop-1"
    assert command.external_product_ref == "item-1"


def test_build_reconcile_price_command_rejects_missing_fields() -> None:
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_price_command(
            operation_id="op-1",
            expected_state={"operation_type": OPERATION_TYPE},
            workspace_id=uuid.uuid4(),
        )
    assert exc.value.code == VALIDATION_FAILED


def test_build_reconcile_price_command_rejects_foreign_operation_type() -> None:
    expected_state = _expected_state()
    expected_state["operation_type"] = "ADJUST_INVENTORY"
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_price_command(
            operation_id="op-1", expected_state=expected_state, workspace_id=uuid.uuid4()
        )
    assert exc.value.code == VALIDATION_FAILED


def test_build_reconcile_price_command_rejects_workspace_mismatch() -> None:
    """持久化 Workspace 与当前 Workspace 不一致 → fail closed,不得改用当前值回查。"""
    expected_state = _expected_state()
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_price_command(
            operation_id="op-1",
            expected_state=expected_state,
            workspace_id=uuid.uuid4(),
        )
    assert exc.value.code == VALIDATION_FAILED


async def test_price_reconciler_delegates_target_price_from_expected_state() -> None:
    gateway = _FixedGateway(ReconcileResult.NOT_APPLIED)
    expected_state = _expected_state()
    reconciler = PriceReconciler(
        gateway,
        workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
        operation_id="op-1",
    )
    result = await reconciler.reconcile(expected_state)
    assert result is ReconcileResult.NOT_APPLIED
    assert gateway.command is not None
    assert gateway.command.expected_price == Decimal("100.00")
    assert gateway.command.operation_id == "op-1"


# ---------------------------------------------------------------------------
# 写入预算验收:通用 Runtime 约束 + Pricing 场景证明
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Uncertain:
    workspace_id: uuid.UUID
    run: Run
    tool_call: ToolCall
    operation: Operation
    state_version: int


async def _seed_uncertain_price_write(
    db_session: AsyncSession, *, execute_attempts: int
) -> _Uncertain:
    """UNKNOWN 改价 Operation 的完整回查上下文(首版无租约)。"""
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    user = User(id=uuid.uuid4(), login=f"user-{uuid.uuid4()}", password_hash="t")
    membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=user.id,
        role=MembershipRole.OPERATOR,
    )
    agent = Agent(id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4()}", name="a")
    agent_version = AgentVersion(
        id=uuid.uuid4(), agent_id=agent.id, version=1, definition_json={}, definition_hash="h"
    )
    model_connection = ModelConnection(
        id=uuid.uuid4(), workspace_id=workspace.id, stable_key=f"m-{uuid.uuid4()}", name="m"
    )
    model_config_version = ModelConfigVersion(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        model_connection_id=model_connection.id,
        version=1,
        backend_type=ModelBackendType.OPENAI,
        base_url="https://api.example.com/v1",
        model="gpt-test",
        non_secret_options={},
        config_hash="h",
    )
    db_session.add_all([workspace, user, agent])
    await db_session.flush()
    db_session.add_all([membership, agent_version, model_connection])
    await db_session.flush()
    db_session.add(model_config_version)
    await db_session.flush()

    session_row = await SessionRepository(db_session).create(
        workspace_id=workspace.id,
        agent_id=agent.id,
        agent_version_id=agent_version.id,
        created_by_actor_id=user.id,
        created_by_membership_id=membership.id,
    )
    run = await RunRepository(db_session).create(
        workspace_id=workspace.id,
        session_id=session_row.id,
        created_by_actor_id=user.id,
        created_by_membership_id=membership.id,
        agent_version_id=agent_version.id,
        model_config_version_id=model_config_version.id,
    )
    await db_session.refresh(run)

    tool_definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        workspace_id=None,
        stable_key=f"tool-{uuid.uuid4()}",
        name="set_listing_price",
    )
    tool_version = ToolVersion(
        id=uuid.uuid4(),
        workspace_id=None,
        tool_id=tool_definition.id,
        version=1,
        effect=ToolEffect.WRITE,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        schema_hash="schema-hash-1",
    )
    message = Message(
        workspace_id=workspace.id,
        session_id=session_row.id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETED,
        content="call set_listing_price",
        sequence_no=1,
    )
    db_session.add_all([tool_definition, tool_version, message])
    await db_session.flush()

    expected_state = _expected_state()
    expected_state["workspace_id"] = str(workspace.id)
    operation_id = uuid.uuid4()
    tool_call = ToolCall(
        workspace_id=workspace.id,
        run_id=run.id,
        message_id=message.id,
        tool_version_id=tool_version.id,
        ordinal=1,
        status=ToolCallStatus.UNKNOWN,
        raw_arguments={"store_ref": "淘宝旗舰店", "sku": "SKU-001"},
        normalized_arguments={"store_ref": "淘宝旗舰店", "sku": "SKU-001"},
    )
    db_session.add(tool_call)
    await db_session.flush()
    operation = Operation(
        workspace_id=workspace.id,
        tool_call_id=tool_call.id,
        operation_id=operation_id,
        idempotency_key=f"op:{operation_id}",
        adapter_key="mock-commerce",
        status="UNKNOWN",
        expected_state=expected_state,
    )
    db_session.add(operation)
    await db_session.flush()
    tool_call.operation_id = operation_id
    for index in range(execute_attempts):
        db_session.add(
            OperationAttempt(
                workspace_id=workspace.id,
                operation_row_id=operation.id,
                attempt_no=index + 1,
                kind=OperationAttemptKind.EXECUTE,
                status=OperationAttemptStatus.SUCCEEDED,
            )
        )
    await db_session.flush()

    # 执行器领取等价操作:QUEUED→RUNNING(首版没有租约,只有状态机)。
    run_service = RunService(session=db_session, workspace_id=workspace.id)
    await db_session.refresh(run)
    started = (
        await db_session.scalars(
            select(Run)
            .where(Run.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).one()
    await run_service.transition_run(
        run_id=run.id,
        target_status=RunStatus.RUNNING,
        event_kind="RUN_STARTED",
        event_payload={"reason": "TEST_SEED"},
        expected_status=RunStatus.QUEUED,
        expected_version=started.state_version,
    )
    await db_session.refresh(run)
    running = (
        await db_session.scalars(
            select(Run)
            .where(Run.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).one()
    await run_service.transition_run(
        run_id=run.id,
        target_status=RunStatus.WAITING_RECONCILIATION,
        event_kind="RUN_WAITING_RECONCILIATION",
        event_payload={"reason": "TOOL_RESULT_UNKNOWN"},
        expected_status=RunStatus.RUNNING,
        expected_version=running.state_version,
    )
    await db_session.refresh(run)
    return _Uncertain(
        workspace_id=workspace.id,
        run=run,
        tool_call=tool_call,
        operation=operation,
        state_version=run.state_version,
    )


@pytest.mark.parametrize(
    "execute_attempts,expected",
    [
        (0, ToolCallStatus.VALIDATING),
        (1, ToolCallStatus.FAILED),
        (2, ToolCallStatus.FAILED),
    ],
)
async def test_price_write_budget_gates_not_applied(
    db_session: AsyncSession, execute_attempts: int, expected: ToolCallStatus
) -> None:
    """同一改价 Operation 的 NOT_APPLIED 重排由 EXECUTE 次数上界裁决。"""
    seed = await _seed_uncertain_price_write(db_session, execute_attempts=execute_attempts)
    adapter = MockCommerceAdapter(db_session)
    service = ReconciliationService(
        db_session,
        workspace_id=seed.workspace_id,
        actor_id=(await _actor_id(db_session, seed.workspace_id)),
        run_id=seed.run.id,
        operation_id=seed.operation.operation_id,
        expected_state_version=seed.state_version,
        reconcile_budget=3,
        write_retry_budget=1,
    )
    outcome = await service.apply_result(ReconcileResult.NOT_APPLIED)
    assert outcome.tool_call_status is expected
    assert outcome.direct_execute is False
    assert outcome.next_step is RunNextStep.ADVANCE
    # 预算上界(1)内不会产生额外的平台逻辑写:本次回查从未调用平台写路径。
    assert await adapter.count_logical_writes(str(seed.operation.operation_id)) == 0


async def _actor_id(db_session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    row = (
        await db_session.scalars(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == workspace_id
            )
        )
    ).first()
    assert row is not None
    return row
