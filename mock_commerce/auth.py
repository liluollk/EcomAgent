"""Mock 平台鉴权模块 — 模拟第三方 API 认证（X-Api-Key 校验）。真实项目换 OAuth 商家 access_token。"""

from fastapi import Header, HTTPException

MOCK_API_KEY = "mock-channel-key"


def verify_api_key(x_api_key: str | None = Header(None)) -> None:
    if x_api_key != MOCK_API_KEY:
        raise HTTPException(
            status_code=401,
            detail={"code": 10003, "message": "鉴权失败：无权限操作"},
        )