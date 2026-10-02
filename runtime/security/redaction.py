"""payload 脱敏(计划 01 Task 6;架构文档 §8、§10)。

``redact`` 是纯函数:对 Mapping 递归检查键名,命中敏感键名集合时值替换为
``[REDACTED]``;list/tuple 逐项处理;标量原样返回。键名匹配大小写不敏感且
按子串命中(例如 ``openai_api_key``、``clientSecret``),方向宁多勿漏;
被误脱敏的非敏感字段无损,漏脱敏的 Secret 即泄漏。供日志、AgentEvent 与
审计(Task 7)统一复用。
"""

from collections.abc import Mapping
from typing import Any, Final


REDACTED_PLACEHOLDER: Final = "[REDACTED]"

_SENSITIVE_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "passwd",
        "password",
        "private_key",
        "secret",
        "secret_key",
        "session_key",
        "token",
    }
)


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).lower()
    return any(name in normalized for name in _SENSITIVE_KEY_NAMES)


def redact(payload: Any) -> Any:
    """递归脱敏 payload;不修改输入,返回新结构。"""
    if isinstance(payload, Mapping):
        return {
            key: REDACTED_PLACEHOLDER if _is_sensitive_key(key) else redact(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list | tuple):
        return [redact(item) for item in payload]
    return payload
