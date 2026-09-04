"""测试 AbortHandler — 中断和重置。"""

import pytest
from agent_runtime.abort_handler import AbortHandler


def test_abort_handler_initial():
    handler = AbortHandler()
    assert not handler.is_aborted
    assert handler.reason == ""


def test_abort_handler_abort():
    handler = AbortHandler()
    handler.abort("user_cancel")
    assert handler.is_aborted
    assert handler.reason == "user_cancel"


def test_abort_handler_reset():
    handler = AbortHandler()
    handler.abort("timeout")
    handler.reset()
    assert not handler.is_aborted
    assert handler.reason == ""