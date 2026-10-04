"""Langfuse 客户端 Protocol 与轻量 HTTP 实现(计划 04 Task 8)。

Runtime 与 Handler 只依赖 :class:`LangfuseClient` Protocol,不依赖 langfuse
SDK 的具体类型;测试注入内存 fake,不发真实网络。``HttpLangfuseClient``
用 httpx 直连 Langfuse 公共 Ingest API(``POST /api/public/ingest``),
不引入 SDK 内部类型,凭据仅用于请求、不写日志。

失败归一(:class:`OutboxError` 子类,由 OutboxWorker 统一按预算重试或置
DEAD;错误消息不携带 payload,避免 Secret 入日志):
- 连接失败/5xx/429 → :class:`LangfuseUnavailableError`(可重试);
- 请求被永久拒绝(4xx)/aggregate 非法 → :class:`LangfuseInvalidTraceError`
  (不可重试)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

import httpx

from runtime.integrations.outbox_worker import OutboxError


@runtime_checkable
class LangfuseClient(Protocol):
    """Langfuse trace-ingest 的最小能力面:按批提交 trace/span 项。"""

    async def ingest(self, batch: Sequence[Mapping[str, object]]) -> None:
        """原子提交一批 trace-ingest 项;失败抛归一的 LangfuseExportError。"""
        ...


class LangfuseExportError(OutboxError):
    """Langfuse 导出失败基类;``retryable`` 标注失败可重试性。"""

    retryable: bool = True


class LangfuseUnavailableError(LangfuseExportError):
    """Langfuse 暂不可用(网络/5xx/429);应按 Outbox 预算延迟重试。"""

    retryable = True


class LangfuseInvalidTraceError(LangfuseExportError):
    """Trace 导出永久无效(Run 缺失/载荷被拒);重试无意义。"""

    retryable = False


class HttpLangfuseClient:
    """基于 httpx 的轻量 Langfuse Ingest 客户端。

    凭据只经 httpx basic auth 发送;异常消息只含状态码,不含响应体与
    payload,避免 Secret 泄漏进日志。
    """

    def __init__(
        self, *, host: str, public_key: str, secret_key: str, timeout: float = 10.0
    ) -> None:
        self._host = host.rstrip("/")
        self._auth = (public_key, secret_key)
        self._timeout = timeout

    async def ingest(self, batch: Sequence[Mapping[str, object]]) -> None:
        try:
            async with httpx.AsyncClient(auth=self._auth, timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._host}/api/public/ingest",
                    json={"batch": [dict(item) for item in batch]},
                )
        except httpx.HTTPError as error:
            raise LangfuseUnavailableError(
                "LANGFUSE_UNAVAILABLE", "langfuse ingest request failed"
            ) from error
        if response.status_code >= 500 or response.status_code == 429:
            raise LangfuseUnavailableError(
                "LANGFUSE_UNAVAILABLE",
                f"langfuse ingest returned status {response.status_code}",
            )
        if response.status_code >= 400:
            raise LangfuseInvalidTraceError(
                "LANGFUSE_REJECTED",
                f"langfuse ingest rejected with status {response.status_code}",
            )


__all__ = [
    "HttpLangfuseClient",
    "LangfuseClient",
    "LangfuseExportError",
    "LangfuseInvalidTraceError",
    "LangfuseUnavailableError",
]
