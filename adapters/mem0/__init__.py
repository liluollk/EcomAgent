"""Mem0 外部记忆索引 Adapter(计划 04 Task 7)。

- ``client``:Mem0Client Protocol 与 OSS 薄封装;
- ``outbox_handler``:把 MEMORY_UPSERT/MEMORY_DELETE Outbox 同步到 Mem0。
"""
