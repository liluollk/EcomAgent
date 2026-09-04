"""测试 AgentEvent 事件的值对象创建和字段验证。"""

import pytest
from events.agent_event import (
    TextDeltaEvent,
    ToolStartEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    TypedErrorEvent,
    StatusEvent,
    CompleteEvent,
    AbortEvent,
    TypedError,
)


def test_text_delta_event():
    event = TextDeltaEvent(text="Hello, 世界")
    assert event.type == "text_delta"
    assert event.text == "Hello, 世界"


def test_text_delta_event_default():
    event = TextDeltaEvent()
    assert event.text == ""


def test_tool_start_event():
    event = ToolStartEvent(
        tool_name="query_inventory",
        tool_use_id="call_001",
        input={"channel": "taobao"},
    )
    assert event.type == "tool_start"
    assert event.tool_name == "query_inventory"
    assert event.tool_use_id == "call_001"
    assert event.input == {"channel": "taobao"}


def test_tool_start_event_default_input():
    event = ToolStartEvent(tool_name="test", tool_use_id="id_1")
    assert event.input == {}


def test_tool_result_event():
    event = ToolResultEvent(
        tool_use_id="call_001",
        tool_name="query_inventory",
        result="库存: 32 件",
        is_error=False,
    )
    assert event.type == "tool_result"
    assert event.result == "库存: 32 件"
    assert not event.is_error


def test_tool_result_event_error():
    event = ToolResultEvent(
        tool_use_id="call_001",
        tool_name="query_inventory",
        result="连接超时",
        is_error=True,
    )
    assert event.is_error


def test_permission_request_event():
    event = PermissionRequestEvent(
        request_id="perm_001",
        tool_name="update_price",
        tool_input={"sku": "BH-201", "new_price": 49.9},
        reason="价格调整需要运营主管确认",
    )
    assert event.type == "permission_request"
    assert event.request_id == "perm_001"
    assert event.tool_input["sku"] == "BH-201"


def test_permission_request_event_default():
    event = PermissionRequestEvent()
    assert event.tool_input == {}


def test_typed_error_event():
    error = TypedError(
        code="API_ERROR",
        title="API 调用失败",
        message="OpenAI API 返回 500 错误",
        can_retry=True,
    )
    event = TypedErrorEvent(error=error)
    assert event.type == "typed_error"
    assert event.error.code == "API_ERROR"
    assert event.error.can_retry


def test_status_event():
    event = StatusEvent(message="正在解析技能...")
    assert event.type == "status"
    assert event.message == "正在解析技能..."


def test_complete_event():
    event = CompleteEvent()
    assert event.type == "complete"


def test_abort_event():
    event = AbortEvent(reason="user_cancel")
    assert event.type == "abort"
    assert event.reason == "user_cancel"


def test_abort_event_default_reason():
    event = AbortEvent()
    assert event.type == "abort"
    assert event.reason == ""


def test_typed_error_dataclass():
    error = TypedError(
        code="PERMISSION_DENIED",
        title="权限不足",
        message="当前角色无法执行此操作",
        can_retry=False,
    )
    assert error.code == "PERMISSION_DENIED"
    assert not error.can_retry