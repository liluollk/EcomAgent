"""execution 包(Operation/ToolResultProcessor/Reconciliation)测试夹具。

提供:

- ``seeded_write_tool_call``:GLOBAL WRITE Tool 版本 + Message + ToolCall(VALIDATING);
- ``running_run``:把 seeded_run 以 state_version CAS 推入 RUNNING(执行器语义);
- ``operation_service``:绑定 running_run 的 OperationService;
- ``uncertain_write``:Operation/ToolCall 均为 UNKNOWN 的写操作,Run 处于
  WAITING_RECONCILIATION;
- ``reconciliation_service``:绑定 uncertain_write 的 ReconciliationService;
- ``tool_result_processor``:绑定固定 Output Schema 的 ToolResultProcessor。
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.execution.operations import OperationService
from runtime.execution.reconciliation import ReconciliationService
from runtime.execution.tool_result_processor import ToolResultProcessor
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    ConfigScope,
    MessageRole,
    MessageStatus,
    ToolEffect,
)
from runtime.persistence.models.configuration import ToolDefinition, ToolVersion
from runtime.persistence.models.execution import Operation, Run, ToolCall
from runtime.persistence.models.interaction import Message
from runtime.persistence.repositories.runs import RunRepository


@dataclass(frozen=True)
class RunningRun:
    """已进入 RUNNING 的 Run:期望 state_version(并发写的唯一凭据)。"""

    run: Run
    state_version: int


@dataclass(frozen=True)
class UncertainWrite:
    """UNKNOWN 写操作与其 Run 的当前 state_version。"""

    run: Run
    tool_call: ToolCall
    operation: Operation
    state_version: int


TEST_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "result": {"type": "string"},
        "record_id": {"type": "string"},
    },
    "required": ["result"],
}


def _write_tool_rows() -> tuple[ToolDefinition, ToolVersion]:
    """GLOBAL 作用域的 WRITE Tool 版本行(tool_calls.tool_version_id 外键)。"""
    tool_definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        workspace_id=None,
        stable_key=f"tool-{uuid.uuid4()}",
        name="update_listing_price",
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
    return tool_definition, tool_version


def _assistant_message(run: Run, sequence_no: int = 1) -> Message:
    """tool_calls.message_id 外键所需的 ASSISTANT 消息。"""
    return Message(
        workspace_id=run.workspace_id,
        session_id=run.session_id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETED,
        content="call update_listing_price",
        sequence_no=sequence_no,
    )


@pytest.fixture()
async def seeded_write_tool_call(
    db_session: AsyncSession, seeded_run: Run
) -> ToolCall:
    """状态 VALIDATING 的写 ToolCall(PreToolUse 已通过,待执行)。"""
    tool_definition, tool_version = _write_tool_rows()
    message = _assistant_message(seeded_run)
    db_session.add_all([tool_definition, tool_version, message])
    await db_session.flush()
    tool_call = ToolCall(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        message_id=message.id,
        tool_version_id=tool_version.id,
        ordinal=1,
        status="VALIDATING",
        raw_arguments={"listing_id": "L1"},
        normalized_arguments={"listing_id": "L1"},
    )
    db_session.add(tool_call)
    await db_session.flush()
    return tool_call


@pytest.fixture()
async def running_run(
    db_session: AsyncSession, seeded_run: Run
) -> RunningRun:
    """以 state_version CAS 把 Run 从 QUEUED 推入 RUNNING(等价执行器领取)。"""
    moved = await RunRepository(db_session).compare_and_set_status(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        expected_version=seeded_run.state_version,
        new_status=RunStatus.RUNNING.value,
        expected_status=RunStatus.QUEUED.value,
    )
    assert moved
    await db_session.refresh(seeded_run)
    return RunningRun(run=seeded_run, state_version=seeded_run.state_version)


@pytest.fixture()
def operation_service(
    db_session: AsyncSession, seeded_workspace, running_run: RunningRun
) -> OperationService:
    """绑定 running_run 的 OperationService(期望 state_version)。"""
    return OperationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=running_run.run.id,
        expected_state_version=running_run.state_version,
    )


@pytest.fixture()
async def uncertain_write(
    db_session: AsyncSession,
    seeded_run: Run,
    run_service,
) -> UncertainWrite:
    """UNKNOWN Operation + WAITING_RECONCILIATION Run 的完整回查上下文。"""
    tool_definition, tool_version = _write_tool_rows()
    message = _assistant_message(seeded_run)
    operation_id = uuid.uuid4()
    db_session.add_all([tool_definition, tool_version, message])
    await db_session.flush()
    # tool_calls.operation_id 复合外键引用 operations 行,须后置回填。
    tool_call = ToolCall(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        message_id=message.id,
        tool_version_id=tool_version.id,
        ordinal=1,
        status="UNKNOWN",
        raw_arguments={"listing_id": "L1"},
        normalized_arguments={"listing_id": "L1"},
    )
    db_session.add(tool_call)
    await db_session.flush()
    operation = Operation(
        workspace_id=seeded_run.workspace_id,
        tool_call_id=tool_call.id,
        operation_id=operation_id,
        idempotency_key=f"op:{operation_id}",
        adapter_key="test-adapter",
        status="UNKNOWN",
    )
    db_session.add(operation)
    await db_session.flush()
    tool_call.operation_id = operation_id
    await db_session.flush()

    # 执行器语义:QUEUED→RUNNING 由 CAS 完成,再迁到 WAITING_RECONCILIATION。
    await RunRepository(db_session).compare_and_set_status(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        expected_version=seeded_run.state_version,
        new_status=RunStatus.RUNNING.value,
        expected_status=RunStatus.QUEUED.value,
    )
    await db_session.refresh(seeded_run)
    running = (await db_session.scalars(select(Run).where(Run.id == seeded_run.id))).one()
    await run_service.transition_run(
        run_id=seeded_run.id,
        target_status=RunStatus.WAITING_RECONCILIATION,
        event_kind="RUN_WAITING_RECONCILIATION",
        event_payload={"reason": "TOOL_RESULT_UNKNOWN"},
        expected_status=RunStatus.RUNNING,
        expected_version=running.state_version,
    )
    await db_session.refresh(seeded_run)
    return UncertainWrite(
        run=seeded_run,
        tool_call=tool_call,
        operation=operation,
        state_version=seeded_run.state_version,
    )


@pytest.fixture()
def reconciliation_service(
    db_session: AsyncSession, seeded_workspace, uncertain_write: UncertainWrite
) -> ReconciliationService:
    """绑定 uncertain_write 的 ReconciliationService(回查预算 3、写入重试预算 1)。

    回查预算 3 容纳两次预算内 UNKNOWN;写入重试预算 1 表示该 Operation 只允许
    一次 EXECUTE,达到后 NOT_APPLIED 不再重排。
    """
    return ReconciliationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=uncertain_write.run.id,
        operation_id=uncertain_write.operation.operation_id,
        expected_state_version=uncertain_write.state_version,
        reconcile_budget=3,
        write_retry_budget=1,
    )


@pytest.fixture()
def tool_result_processor() -> ToolResultProcessor:
    """绑定固定 Output Schema 的 ToolResultProcessor(纯逻辑,无数据库)。"""
    return ToolResultProcessor(output_schema=TEST_OUTPUT_SCHEMA)
