"""平台 Adapter(``adapters.commerce``)。

实现商品运营 Agent 依赖的平台协议(架构文档 §2:平台协议由 Agent/Adapter
侧实现,Runtime 不感知)。Mock 实现以 ``product_operations`` 的业务表为
平台权威状态,并支持确定性故障注入;REST 实现只声明定价能力。

具体实现模块需要显式导入(``adapters.commerce.mock_adapter`` /
``adapters.commerce.rest_adapter``);此处只导出"能力范围"这一类装配期需要
的元信息,供组合根判断某个 Adapter 能承担哪些业务能力。
"""

from adapters.commerce.rest_adapter import (
    CAPABILITIES,
    CapabilityUnsupportedError,
    RestCommerceAdapter,
)


__all__ = [
    "CAPABILITIES",
    "CapabilityUnsupportedError",
    "RestCommerceAdapter",
]
