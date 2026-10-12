"""参数可信等级的唯一类型来源(计划 04 Task 8;架构文档 §5)。

``ArgumentTrustLevel`` 只在此处定义一次:Runtime 的
:class:`~runtime.execution.pretool.decisions.ArgumentProvenance` 与业务的
证据解析都复用它。此前业务模块各自定义同名 ``str`` 枚举,一旦守卫改用字符串
比较,拼写错误(例如 ``"TRUSTED_BY_MODEL"``)就无法被静态拦截。收敛到单一
类型后,未知等级在构造值对象时即被拒绝。

可信等级只由确定性规则判定,不接受模型自报来源。
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class ArgumentTrustLevel(str, Enum):
    """参数来源的可信等级(架构文档 §5 的五种来源)。"""

    USER_EXPLICIT = "USER_EXPLICIT"
    USER_CONFIRMED = "USER_CONFIRMED"
    SYSTEM_RESOLVED = "SYSTEM_RESOLVED"
    DETERMINISTIC_DERIVED = "DETERMINISTIC_DERIVED"
    UNTRUSTED = "UNTRUSTED"


TRUSTED_TRUST_LEVELS: Final[frozenset[ArgumentTrustLevel]] = frozenset(
    {
        ArgumentTrustLevel.USER_EXPLICIT,
        ArgumentTrustLevel.USER_CONFIRMED,
        ArgumentTrustLevel.SYSTEM_RESOLVED,
        ArgumentTrustLevel.DETERMINISTIC_DERIVED,
    }
)
"""可信白名单;不在其中(含字段缺省)一律按不可信处理。"""


__all__ = ["TRUSTED_TRUST_LEVELS", "ArgumentTrustLevel"]
