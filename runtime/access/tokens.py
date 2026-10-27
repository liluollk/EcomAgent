"""短期 JWT 签发与校验(计划 01 Task 5;架构文档 §2)。

不变式:
- claim 集合精确为 ``{sub, iat, exp, jti}``;角色、权限与资源范围永不写入
  token,校验时遇到多余 claim 一律拒绝;
- 算法固定 HS256,校验拒绝 ``alg=none`` 与其它算法;
- secret 从环境变量 ``RUNTIME_JWT_SECRET`` 读取;缺失即抛
  :class:`SecretNotConfigured`,不存在可用的默认密钥。
"""

import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import jwt
from pydantic import BaseModel, ConfigDict


SECRET_ENV_VAR: Final = "RUNTIME_JWT_SECRET"
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


class SecretNotConfigured(Exception):
    """未配置 JWT 签名密钥:启动或签发前即失败,不回退到公开默认值。"""


def resolve_secret(environ: Mapping[str, str] | None = None) -> str:
    """读取签名密钥;缺失即抛错,不提供可用的默认值。

    ``environ`` 供启动配置在一次性读取环境时复用同一份校验,默认读进程环境。
    """
    source = os.environ if environ is None else environ
    secret = source.get(SECRET_ENV_VAR)
    if secret:
        return secret
    raise SecretNotConfigured(
        f"environment variable {SECRET_ENV_VAR} is not set; "
        "there is no usable default secret"
    )


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
