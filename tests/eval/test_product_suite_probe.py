"""Provider 探测必须发真实对话请求,而不是只探 ``GET /models``。

2026-10-25 的 v4 复测暴露过这个盲区:模型列表接口照常返回 200,而每次对话都
回 ``402 insufficient balance``,于是 ``core`` 分区 96 个用例全部烧成
``PROVIDER_ERROR`` 才结束。探测改发一次 ``max_tokens=16`` 的补全后,这类
"列表能看、对话不能发"的问题在第一个 case 之前就停下。

这里只覆盖探测函数本身:不建客户端、不发网络。
"""

from __future__ import annotations

from typing import Any

from eval.config import fixed_eval_config
from eval.product_suite import _probe_provider


class _StatusError(Exception):
    """模拟 SDK 的 APIStatusError:异常自身带 HTTP 状态码。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Error code: {status_code} - {{'message': 'quota'}}")
        self.status_code = status_code


class _Completions:
    def __init__(self, error: BaseException | None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return object()


class _StubClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.chat = type("_Chat", (), {"completions": _Completions(error)})()

    @property
    def completions(self) -> _Completions:
        return self.chat.completions


async def test_probe_sends_a_real_completion() -> None:
    """探测打的是对话接口,带固定 model 与 thinking 开关。"""
    config = fixed_eval_config()
    client = _StubClient()

    assert await _probe_provider(client, config) is None  # type: ignore[arg-type]

    calls = client.completions.calls
    assert len(calls) == 1
    assert calls[0]["model"] == config.model
    assert calls[0]["max_tokens"] == 16
    assert calls[0]["extra_body"] == {"thinking": {"type": config.thinking_mode}}


async def test_probe_surfaces_status_code_of_quota_failures() -> None:
    """402 不能被吞成"未知失败":状态码要进报告,否则分不清额度与代码问题。"""
    detail = await _probe_provider(_StubClient(_StatusError(402)), fixed_eval_config())  # type: ignore[arg-type]

    assert detail is not None
    assert detail.startswith("provider probe failed:")
    assert "HTTP 402" in detail
    assert "_StatusError" in detail


async def test_probe_reports_errors_without_status_code() -> None:
    """连接类异常没有状态码:照常给出类型与消息,不拼空的 HTTP 段。"""
    detail = await _probe_provider(
        _StubClient(ConnectionError("tunnel refused")),  # type: ignore[arg-type]
        fixed_eval_config(),
    )

    assert detail is not None
    assert "HTTP" not in detail
    assert "ConnectionError" in detail
    assert "tunnel refused" in detail
