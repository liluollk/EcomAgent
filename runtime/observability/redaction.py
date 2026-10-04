"""Trace 导出字段级脱敏(计划 04 Task 8;执行状态文档 §"Langfuse Trace")。

``TraceRedactor`` 在 :func:`runtime.security.redaction.redact` 的敏感键名
脱敏(大小写不敏感子串命中,如 ``openai_api_key``/``clientSecret``)之上,
增加**密钥形态值**识别:无论键名为何,字符串中形如 ``sk-...``/``AKIA…``/
``gh[pousr]_...``/``Bearer ...`` 的密钥子串一律替换为 ``[REDACTED]``。

规则确定可测、纯函数、不改输入;方向宁多勿漏——被误脱敏的非敏感字段无损,
漏脱敏的 Secret 即泄漏。模型密钥、认证 Header 与业务敏感字段不得写入
Langfuse Trace(执行状态文档),故导出 payload(messages/payload/attributes
等键路径之下)全部经本脱敏器。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from runtime.security.redaction import REDACTED_PLACEHOLDER, redact


SECRET_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)
"""密钥形态值模式:命中子串(而非整个值)替换,保证序列化后不含原密钥。"""


class TraceRedactor:
    """Trace payload 双层脱敏器:先敏感键名,后密钥形态值。"""

    def redact(self, payload: Any) -> Any:
        """递归脱敏 payload;不修改输入,返回新结构。"""
        return self._scrub_secret_shapes(redact(payload))

    def _scrub_secret_shapes(self, payload: Any) -> Any:
        if isinstance(payload, Mapping):
            return {
                key: self._scrub_secret_shapes(value) for key, value in payload.items()
            }
        if isinstance(payload, list | tuple):
            return [self._scrub_secret_shapes(item) for item in payload]
        if isinstance(payload, str):
            for pattern in SECRET_VALUE_PATTERNS:
                payload = pattern.sub(REDACTED_PLACEHOLDER, payload)
        return payload


__all__ = ["REDACTED_PLACEHOLDER", "SECRET_VALUE_PATTERNS", "TraceRedactor"]
