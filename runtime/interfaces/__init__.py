"""Runtime 公共接口层。

按架构文档 §2 依赖规则,本包只包含 Pydantic 数据结构、Enum 和 Protocol,
不导入 SQLAlchemy、FastAPI、具体 Provider SDK 或业务 Agent。
"""
