"""ChannelRestClient — 渠道平台 REST 客户端抽象（真实 HTTP 协议层）。

单次请求语义：一次 call = 一次真实 HTTP 请求，失败即抛 RestApiError。
重试/超时/幂等键生成属于 Execution Policy 职责（execution/policy.py），
客户端不做任何重试——保证「重试链路在策略层可观测、可分类、可计数」。
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx

PLATFORM_OK = 0

PLATFORM_ERROR_TEXT = {
    10001: "参数校验失败",
    10002: "渠道不存在",
    10003: "无权限操作",
    10004: "业务状态不允许（如促销已存在）",
    10005: "平台限流，请稍后重试",
}


class RestApiError(Exception):
    """平台 API 调用失败的统一异常。code 为平台业务码。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChannelRestClient:
    """渠道 REST 客户端：url 拼装、鉴权、错误映射（单次请求）。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18080",
        auth_header: Optional[dict[str, str]] = None,
        default_headers: Optional[dict[str, str]] = None,
        timeout: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {
            **(default_headers if default_headers is not None else {"X-Api-Key": "mock-channel-key"}),
            **(auth_header or {}),
        }
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """执行一次平台请求：成功返回 data，失败抛 RestApiError（带平台业务码）。"""
        if method.upper() == "POST" and idempotency_key is None:
            idempotency_key = uuid.uuid4().hex
        headers = dict(self._headers)
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        try:
            resp = await self._client.request(method, path, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as e:
            # 连接失败视为瞬态（10005），由 Execution Policy 按分类决定重试
            raise RestApiError(10005, f"平台连接失败：{e}") from e

        try:
            payload = resp.json()
        except ValueError as e:
            raise RestApiError(10001, f"平台响应非 JSON：{resp.status_code}") from e

        if not isinstance(payload, dict):
            raise RestApiError(10001, f"平台响应结构异常：{type(payload).__name__}")
        if "code" not in payload and isinstance(payload.get("detail"), dict):
            payload = payload["detail"]

        code = payload.get("code", -1)
        if code != PLATFORM_OK:
            message = payload.get("message") or PLATFORM_ERROR_TEXT.get(code, f"未知平台错误码 {code}")
            raise RestApiError(code, message)
        return payload.get("data", {})

    async def call_raw(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """执行一次平台请求，返回平台「原始 JSON」（信封/错误都在 body 内）。

        与 call() 的区别：call() 强解 {code,message,data} 信封并抛出 RestApiError，
        无法解析 TOP 风格的 item_sku_*_response / error_response。call_raw 不做任何
        解包，把信封解析与错误映射交给各 Adapter。

        网络/传输错误（超时、连接失败）与 HTTP >= 400 会以 httpx 异常透传，
        交由 adapter 做平台特定的错误分类（如写超时需要 side_effect_possible）。
        平台业务错误（错误码在 body 内、HTTP 200）不会在此抛错——那是 adapter 职责。
        """
        if method.upper() == "POST" and idempotency_key is None:
            idempotency_key = uuid.uuid4().hex
        headers = dict(self._headers)
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        try:
            resp = await self._client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.HTTPError:
            raise  # 超时 / 连接失败等直接透传给 adapter

        if resp.status_code >= 400:
            resp.raise_for_status()  # 触发 httpx.HTTPStatusError，交由 adapter 分类

        try:
            return resp.json()
        except ValueError as e:
            raise RestApiError(10001, f"平台响应非 JSON：{resp.status_code}") from e


DEFAULT_PLATFORM_URL = "http://127.0.0.1:18080"

_default_client: Optional[ChannelRestClient] = None


def get_rest_client(
    base_url: str | None = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ChannelRestClient:
    global _default_client
    from os import environ

    url = base_url or environ.get("CHANNEL_API_URL") or DEFAULT_PLATFORM_URL
    env_changed = (
        _default_client is not None
        and _default_client._base_url != url.rstrip("/")
    )
    if _default_client is None or base_url is not None or transport is not None or env_changed:
        if _default_client is not None:
            try:
                import asyncio as _asyncio

                _asyncio.create_task(_default_client.aclose())
            except RuntimeError:
                pass

        if transport is None and not environ.get("CHANNEL_API_URL"):
            from httpx import ASGITransport as _ASGITransport

            from mock_commerce.routes import app as _api_app

            transport = _ASGITransport(_api_app)
        _default_client = ChannelRestClient(base_url=url, transport=transport)
    return _default_client
