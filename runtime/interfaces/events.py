"""AgentEvent 公共类型占位(架构文档 §9/§10)。

两份文档均未定义 AgentEvent 的字段结构:事件表结构由持久化阶段
(计划 01 Task 3 的 agent_events 表)定义,"只有 Runtime 应用服务能够执行
状态迁移并写 AgentEvent"的语义由 Harness 阶段实现。在文档给出字段定义前,
本模块不提前发明任何公共模型。
"""
