"""ChannelRestClient — 渠道平台 REST 客户端抽象（真实 HTTP 协议层）。

设计目标（对应”真实接入四件事“的口径）：
  1. 协议层真实：handler → HTTP 请求 → 平台端点，本类负责真实网络往返。
  2. 鉴权注入点：`auth_header` 由调用方提供 —— 真实项目此处换 OAuth 商家
     access_token（当前为 mock 平台的模拟 key，业务数据仍 Mock）。
  3. 错误映射：平台错误码 / HTTP 状态 → RestApiError，由上层转成
     is_error=True 的结果文本，让 LLM 可理解可解释。
  4. transport 可注入：测试走 httpx 的 ASGI/模拟 transport（离线），
     运行时走真实 TCP；换真实渠道只需替换 base_url + auth 实现。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

import httpx

# 平台返回体约定的业务码
PLATFORM_OK = 0

# 平台错误码 → 中文语义（转给上层拼 is_error 文本）
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
    """渠道 REST 客户端：url 拼装、鉴权、超时、错误映射。"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18080",
        auth_header: Optional[dict[str, str]] = None,
        default_headers: Optional[dict[str, str]] = None,
        timeout: float = 5.0,
        retries: int = 2,
        retry_delay: float = 0.2,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # default_headers 为 None 时默认携带 mock 平台模拟 key（历史行为）；
        # 真实平台渠道传 {}（或 OAuth 头），避免向真实平台泄漏 mock key。
        self._headers = {
            **(default_headers if default_headers is not None else {"X-Api-Key": "mock-channel-key"}),
            **(auth_header or {}),
        }
        self._retries = retries
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        """关闭底层连接池。"""
        await self._client.aclose()

    async def call(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """执行一次平台 API 调用。

        Args:
            method: HTTP 方法（GET / POST / PUT / DELETE）。
            path: 请求路径，如 "/v1/taobao/inventory"。
            params: URL 查询参数。
            json_body: JSON 请求体。
            idempotency_key: 幂等键（X-Idempotency-Key 头）。POST 写操作默认
                自动生成，并在限流/连接失败重试时复用同一键，防止重试重复创建。

        Returns:
            dict: 平台业务字段（{"code": 0, "data": {...}} 中的 data）。

        Raises:
            RestApiError: 平台业务码非 0 或 HTTP 出错（重试后仍失败）。
        """
        if method.upper() == "POST" and idempotency_key is None:
            idempotency_key = uuid.uuid4().hex
        headers = dict(self._headers)
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        last_error: Optional[RestApiError] = None
        # 轻量重试：连接失败 / 平台限流（10005）最多重试 2 次，复用同一幂等键
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.request(method, path, params=params, json=json_body, headers=headers)
            except httpx.HTTPError as e:
                last_error = RestApiError(10005, f"平台连接失败：{e}")
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_delay)
                    continue
                break

            try:
                payload = resp.json()
            except ValueError as e:
                raise RestApiError(10001, f"平台响应非 JSON：{resp.status_code}") from e

            # 平台响应归一化：部分平台把业务体包在 detail（如 HTTPException）下，
            # 统一到顶层 {code, message, data} 后再解析
            if not isinstance(payload, dict):
                raise RestApiError(10001, f"平台响应结构异常：{type(payload).__name__}")
            if "code" not in payload and isinstance(payload.get("detail"), dict):
                payload = payload["detail"]

            code = payload.get("code", -1)
            if code != PLATFORM_OK:
                message = payload.get("message") or PLATFORM_ERROR_TEXT.get(code, f"未知平台错误码 {code}")
                err = RestApiError(code, message)
                if code == 10005 and attempt < self._retries:
                    last_error = err
                    await asyncio.sleep(self._retry_delay)
                    continue
                raise err
            return payload.get("data", {})

        assert last_error is not None
        raise last_error


# 模块级默认客户端：默认连本地 mock 平台服务（端口可用环境变量覆盖）
DEFAULT_PLATFORM_URL = "http://127.0.0.1:18080"

_default_client: Optional[ChannelRestClient] = None


def get_rest_client(
    base_url: str | None = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ChannelRestClient:
    """获取（或按需重建）默认渠道 REST 客户端。

    Args:
        base_url: 覆盖默认平台地址；None 表示沿用当前实例（首次创建默认）。
        transport: 注入测试用 transport（httpx.MockTransport / ASGITransport）。

    Returns:
        ChannelRestClient: 单例客户端。

    离线兜底：未配置 CHANNEL_API_URL 时自动注入 ASGITransport 直连 mock 平台
    app（HTTP 语义真实走 httpx，无 TCP socket），保证 MCP 单测与运行时
    未拉起平台服务时工具仍返回真实业务数据；配置 CHANNEL_API_URL 后走真实 TCP。
    """
    global _default_client
    from os import environ

    url = base_url or environ.get("CHANNEL_API_URL") or DEFAULT_PLATFORM_URL
    # 单例重建条件：未初始化 / 显式覆盖 base_url 或 transport / 环境变量变化
    env_changed = (
        _default_client is not None
        and _default_client._base_url != url.rstrip("/")
    )
    if _default_client is None or base_url is not None or transport is not None or env_changed:
        if _default_client is not None:
            # 重建（换 base_url / transport）时确保旧连接关闭
            try:
                import asyncio

                asyncio.create_task(_default_client.aclose())
            except RuntimeError:
                pass

        if transport is None and not environ.get("CHANNEL_API_URL"):
            # 离线路径：直连 mock 平台 app（ASGI），不经过 TCP
            from httpx import ASGITransport as _ASGITransport

            from mocks.channel_api_mock import app as _api_app

            transport = _ASGITransport(_api_app)
        _default_client = ChannelRestClient(base_url=url, transport=transport)
    return _default_client