"""Langfuse Trace 导出适配器(计划 04 Task 8):非权威外部可观测能力。

Langfuse 故障只影响 Trace 导出(Outbox 重试/DEAD),不影响 Runtime 权威
状态与授权结果;导出 payload 必须先经 :class:`runtime.observability.redaction.TraceRedactor`
字段级脱敏。
"""
