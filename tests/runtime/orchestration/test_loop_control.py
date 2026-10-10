"""Agent Loop 的纯控制流不变量测试。"""

from types import SimpleNamespace

from runtime.orchestration.agent_loop import AgentLoop


def test_successful_tool_calls_require_a_follow_up_model_round() -> None:
    outcome = SimpleNamespace(tool_calls=(object(),))

    assert AgentLoop._needs_model_followup(outcome) is True


def test_text_only_completion_does_not_require_a_follow_up_round() -> None:
    outcome = SimpleNamespace(tool_calls=())

    assert AgentLoop._needs_model_followup(outcome) is False
