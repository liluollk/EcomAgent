"""Runtime 只追加审计(计划 01 Task 7;架构文档《身份与 Workspace 权限设计》§10)。

AuditService 只提供 ``append`` 与按 Workspace/关联 ID 的读取;审计事件不可修改、
不可删除,payload 入库前脱敏。后续配置、Request 与写操作应用服务均通过该包写审计。
"""

from runtime.audit.models import AppendAuditEvent, AuditEventView
from runtime.audit.redaction import redact_audit_payload
from runtime.audit.service import AuditMutationRejected, AuditService


__all__ = [
    "AppendAuditEvent",
    "AuditEventView",
    "AuditMutationRejected",
    "AuditService",
    "redact_audit_payload",
]
