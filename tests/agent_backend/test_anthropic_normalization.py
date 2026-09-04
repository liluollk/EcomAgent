"""测试 Anthropic 后端消息归一化 — OpenAI 格式到 Anthropic 格式的转换。"""

from agent_backend.anthropic_backend import _normalize_openai_messages


def test_plain_text_and_system_extraction():
    """纯文本消息透传为内容块结构，system 消息提取为 system 文本。"""
    messages = [
        {"role": "system", "content": "内部补充说明"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
        {"role": "user", "content": "再见"},
    ]
    normalized, system = _normalize_openai_messages(messages)

    assert system == "内部补充说明"
    assert all(m["role"] in ("user", "assistant") for m in normalized)
    assert normalized[0] == {"role": "user", "content": [{"type": "text", "text": "你好"}]}
    assert normalized[1] == {"role": "assistant", "content": [{"type": "text", "text": "你好！"}]}
    assert normalized[2]["content"][0]["text"] == "再见"


def test_tool_calls_and_results_normalized():
    """assistant(tool_calls) 转 tool_use 内容块，连续 role=tool 合并为一条 user 消息。"""
    messages = [
        {"role": "user", "content": "调价"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "update_price", "arguments": "{\"sku\": \"S1\", \"new_price\": 99}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "query_inventory", "arguments": "not-json"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "已更新"},
        {"role": "tool", "tool_call_id": "call_2", "content": "库存 100"},
    ]
    normalized, system = _normalize_openai_messages(messages)

    assert system == ""
    assistant = normalized[1]
    assert assistant["role"] == "assistant"
    blocks = assistant["content"]
    # 无文本时不应产生空 text 块
    assert all(b["type"] == "tool_use" for b in blocks)
    assert blocks[0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "update_price",
        "input": {"sku": "S1", "new_price": 99},
    }
    # 非法 JSON 参数回退为空对象，保证 tool_use.input 始终为 dict
    assert blocks[1]["input"] == {}

    result_msg = normalized[2]
    assert result_msg["role"] == "user"
    assert len(result_msg["content"]) == 2
    assert result_msg["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "已更新",
    }
    assert result_msg["content"][1]["tool_use_id"] == "call_2"
    assert result_msg["content"][1]["content"] == "库存 100"


def test_text_between_tool_results_not_merged():
    """tool 结果与后续文本消息之间不应错误合并。"""
    messages = [
        {"role": "user", "content": "查询"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "query_inventory", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果A"},
        {"role": "user", "content": "再查一次"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "query_inventory", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c2", "content": "结果B"},
    ]
    normalized, _ = _normalize_openai_messages(messages)

    roles = [m["role"] for m in normalized]
    assert roles == ["user", "assistant", "user", "user", "assistant", "user"]
    assert normalized[2]["content"][0]["content"] == "结果A"
    assert normalized[3]["content"][0]["text"] == "再查一次"
    assert normalized[5]["content"][0]["tool_use_id"] == "c2"
