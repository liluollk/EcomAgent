"""平台 Adapter(``adapters.commerce``)。

实现商品运营 Agent 依赖的平台协议(架构文档 §2:平台协议由 Agent/Adapter
侧实现,Runtime 不感知)。Mock 实现以 ``product_operations`` 的业务表为
平台权威状态,并支持确定性故障注入。
"""
