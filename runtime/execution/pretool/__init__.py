"""固定 PreToolUse 管线(计划 02 Task 6;架构文档 §9)。

- ``decisions``:统一决策种类、原因码与各阶段值对象;
- ``schema_validation``:基于 jsonschema 的 Runtime Schema Validator;
- ``pipeline``:固定 10 阶段的装配与执行(顺序不可重排,任一阶段决策即短路)。
"""
