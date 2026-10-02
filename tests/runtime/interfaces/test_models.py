"""Runtime 公共类型不变式测试(计划 01 Task 2 Step 1)。

前三个用例为计划文档原文;其余为架构文档写明的不变式补充。
"""

import uuid
from collections.abc import Callable
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from runtime.interfaces.identifiers import (
    ActorId,
    OperationId,
    ResourceScope,
    RunId,
    SessionId,
    ToolCallId,
    WorkspaceId,
)
from runtime.interfaces.state import AssistantMessageStatus, MessageType, RunStatus
from runtime.interfaces.tools import ToolDefinition, ToolEffect


def test_tool_definition_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ToolDefinition(
            stable_key="update_price",
            version=1,
            description="update price",
            input_json_schema={"type": "object"},
            output_json_schema={"type": "object"},
            effect=ToolEffect.WRITE,
            required_permissions=frozenset({"product.price.write"}),
            source_kind="LOCAL",
            schema_hash="abc",
            unexpected=True,
        )


def test_waiting_reconciliation_is_active() -> None:
    assert RunStatus.WAITING_RECONCILIATION.is_terminal is False


def test_selected_resource_scope_requires_ids() -> None:
    with pytest.raises(ValidationError):
        ResourceScope(mode="SELECTED", resource_ids=frozenset())


def test_run_status_members_match_architecture() -> None:
    assert {status.value for status in RunStatus} == {
        "QUEUED",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_CONFIRMATION",
        "WAITING_RECONCILIATION",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }


def test_only_completed_failed_cancelled_are_terminal() -> None:
    assert {status for status in RunStatus if status.is_terminal} == {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }


def test_message_types_match_architecture() -> None:
    assert {message_type.value for message_type in MessageType} == {
        "USER",
        "ASSISTANT",
        "TOOL",
        "SESSION_SUMMARY",
    }


def test_assistant_message_status_match_architecture() -> None:
    assert {status.value for status in AssistantMessageStatus} == {
        "STREAMING",
        "COMPLETED",
        "ABORTED",
    }


def test_tool_effect_has_read_and_write() -> None:
    assert {effect.value for effect in ToolEffect} == {"READ", "WRITE"}


def test_tool_definition_is_frozen() -> None:
    definition = ToolDefinition(
        stable_key="update_price",
        version=1,
        description="update price",
        input_json_schema={"type": "object"},
        output_json_schema={"type": "object"},
        effect=ToolEffect.READ,
        required_permissions=frozenset(),
        source_kind="LOCAL",
        schema_hash="abc",
    )
    with pytest.raises(ValidationError):
        definition.description = "mutated"


def test_all_scope_allows_empty_ids() -> None:
    scope = ResourceScope(mode="ALL")
    assert scope.resource_ids == frozenset()


def test_resource_scope_is_frozen_and_forbids_unknown_fields() -> None:
    scope = ResourceScope(mode="SELECTED", resource_ids=frozenset({"store-1"}))
    with pytest.raises(ValidationError):
        scope.mode = "ALL"
    with pytest.raises(ValidationError):
        ResourceScope(mode="ALL", unexpected=1)


@pytest.mark.parametrize(
    "identifier_type",
    [WorkspaceId, SessionId, RunId, ToolCallId, OperationId, ActorId],
)
def test_identifier_newtypes_wrap_uuid(identifier_type: Callable[[UUID], UUID]) -> None:
    raw = uuid.uuid4()
    value = identifier_type(raw)
    assert isinstance(value, UUID)
    assert value == raw


def test_identifier_newtypes_validate_in_pydantic_models() -> None:
    class Probe(BaseModel):
        workspace_id: WorkspaceId
        session_id: SessionId

    workspace_id = "01937a6a-7b52-7d50-8f60-1a2b3c4d5e6f"
    session_id = str(uuid.uuid4())
    probe = Probe(workspace_id=workspace_id, session_id=session_id)
    assert probe.workspace_id == UUID(workspace_id)
    assert probe.session_id == UUID(session_id)
