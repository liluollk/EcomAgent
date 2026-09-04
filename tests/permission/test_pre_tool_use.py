"""测试 PreToolUsePipeline — 可插拔检查器链。"""

import pytest
from permission.pre_tool_use import (
    PreToolUsePipeline,
    PreToolUseAction,
    PreToolUseResult,
)


def test_pipeline_allow():
    pipeline = PreToolUsePipeline()
    result = pipeline.check("query_inventory", {"channel": "taobao"})
    assert result.action == PreToolUseAction.ALLOW


def test_pipeline_block():
    def block_all(tool_name, tool_input):
        return PreToolUseResult(
            action=PreToolUseAction.BLOCK,
            reason="测试拦截",
        )
    pipeline = PreToolUsePipeline()
    pipeline.add_checker(block_all)
    result = pipeline.check("update_price", {"new_price": 10})
    assert result.action == PreToolUseAction.BLOCK
    assert result.reason == "测试拦截"


def test_pipeline_ask():
    def ask_all(tool_name, tool_input):
        return PreToolUseResult(
            action=PreToolUseAction.ASK,
            reason="需要确认",
        )
    pipeline = PreToolUsePipeline()
    pipeline.add_checker(ask_all)
    result = pipeline.check("update_price", {})
    assert result.action == PreToolUseAction.ASK


def test_pipeline_short_circuit():
    """测试短路逻辑：第一个拦截后不再执行后续检查器。"""
    call_count = [0]

    def block_first(tool_name, tool_input):
        return PreToolUseResult(action=PreToolUseAction.BLOCK)

    def counter(tool_name, tool_input):
        call_count[0] += 1
        return PreToolUseResult(action=PreToolUseAction.ALLOW)

    pipeline = PreToolUsePipeline()
    pipeline.add_checker(block_first)
    pipeline.add_checker(counter)
    result = pipeline.check("test", {})
    assert result.action == PreToolUseAction.BLOCK
    assert call_count[0] == 0  # 第二个检查器不应该被调用


def test_pipeline_remove_checker():
    def block_all(tool_name, tool_input):
        return PreToolUseResult(action=PreToolUseAction.BLOCK)

    pipeline = PreToolUsePipeline()
    pipeline.add_checker(block_all)
    pipeline.remove_checker(block_all)
    result = pipeline.check("test", {})
    assert result.action == PreToolUseAction.ALLOW