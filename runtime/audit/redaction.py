"""审计域 payload 脱敏(计划 01 Task 7)。

直接复用 ``runtime.security.redaction`` 的递归字段级脱敏——审计与日志、
AgentEvent 共用同一敏感键策略(键名大小写不敏感、按子串命中,宁多勿漏),
本模块只提供审计语义的统一入口与再导出,不重复实现脱敏逻辑。
"""

from typing import Any, cast

from runtime.security.redaction import REDACTED_PLACEHOLDER, redact


__all__ = ["REDACTED_PLACEHOLDER", "redact", "redact_audit_payload"]


def redact_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """审计 payload 入库前的唯一脱敏入口;敏感键值统一替换为 [REDACTED]。"""
    return cast("dict[str, Any]", redact(payload))
