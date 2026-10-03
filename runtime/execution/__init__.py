"""Runtime 执行内核(计划 02)。

- ``transitions``:Run 显式状态迁移表与校验;
- ``run_service``:状态迁移 + AgentEvent 同事务组合服务;
- ``pretool``:固定 10 阶段 PreToolUse 管线;
- ``tool_registry``:Run 钉住的 Tool 版本解析;
- ``operations``:两阶段写登记(稳定 Operation)与结果提交(fencing + 分类);
- ``tool_result_processor``:固定 PostToolUse 管线(协议/分类/Schema/脱敏/投影);
- ``context_projection``:模型投影构建器(确定性裁剪 + content_hash);
- ``tool_executor``:两阶段执行编排(begin → handler → record_result);
- ``reconciliation``:RECONCILE_OPERATION 回查(NOT_APPLIED 只能回 VALIDATING)。
"""
