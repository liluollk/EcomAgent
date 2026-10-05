"""Eval 报告层(计划 05 Task 7):JSON 报告、Markdown 视图与 Langfuse 导出。

报告只呈现分项结果(任务完成/行为决策/写安全/故障恢复),绝无综合总分;
凭据经 :class:`runtime.observability.redaction.TraceRedactor` 双层脱敏
(敏感键名 + 密钥形态)后才进入报告与导出 payload。
"""
