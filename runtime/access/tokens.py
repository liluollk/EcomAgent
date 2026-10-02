"""短期 JWT 签发与校验(计划 01 Task 5;架构文档 §2)。

不变式:
- claim 集合精确为 ``{sub, iat, exp, jti}``;角色、权限与资源范围永不写入
  token,校验时遇到多余 claim 一律拒绝;
- 算法固定 HS256,校验拒绝 ``alg=none`` 与其它算法;
- secret 从环境变量 ``RUNTIME_JWT_SECRET`` 读取;默认值仅供本地测试,
  不承载生产语义。
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import jwt
from pydantic import BaseModel, ConfigDict


SECRET_ENV_VAR: Final = "RUNTIME_JWT_SECRET"
LOCAL_TEST_SECRET: Final = "local-test-secret-not-for-production"
DEFAULT_TTL: Final = timedelta(minutes=30)

_ALGORITHM: Final = "HS256"
_CLAIMS: Final = frozenset({"sub", "iat", "exp", "jti"})


class TokenRejected(Exception):
    """token 无法信任:签名无效、算法不符、过期或 claim 不合规。"""


class VerifiedToken(BaseModel):
    """JWT 校验结果;只包含身份定位信息,不含可长期信任的授权事实。"""

    model_config = ConfigDict(frozen=True)

    actor_id: UUID
    issued_at: datetime
    expires_at: datetime
    token_id: str


def resolve_secret() -> str:
    """读取签名密钥;未配置环境变量时返回本地测试默认值。"""
    secret = os.environ.get(SECRET_ENV_VAR)
    if secret:
        return secret
    return LOCAL_TEST_SECRET


def issue_token(
    actor_id: UUID,
    *,
    secret: str | None = None,
    now: datetime | None = None,
    expires_in: timedelta = DEFAULT_TTL,
) -> str:
    """为 actor 签发短期 JWT;claim 仅 sub/iat/exp/jti。"""
    issued_at = now if now is not None else datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(actor_id),
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + expires_in).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload, secret if secret is not None else resolve_secret(), algorithm=_ALGORITHM
    )


def verify_token(token: str, *, secret: str | None = None) -> VerifiedToken:
    """校验 JWT 并返回 actor 身份;任何不可信情况抛出 TokenRejected。"""
    try:
        payload = jwt.decode(
            token,
            secret if secret is not None else resolve_secret(),
            algorithms=[_ALGORITHM],
            options={"require": sorted(_CLAIMS)},
        )
    except jwt.InvalidTokenError as error:
        raise TokenRejected(str(error)) from error

    unexpected = set(payload) - _CLAIMS
    if unexpected:
        raise TokenRejected(f"unexpected claims: {sorted(unexpected)}")

    try:
        actor_id = UUID(str(payload["sub"]))
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (TypeError, ValueError) as error:
        raise TokenRejected(f"invalid claim value: {error}") from error

    token_id = str(payload["jti"])
    if not token_id:
        raise TokenRejected("empty jti")

    return VerifiedToken(
        actor_id=actor_id, issued_at=issued_at, expires_at=expires_at, token_id=token_id
    )
