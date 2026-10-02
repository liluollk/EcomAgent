"""持久层公共基类、MixIn 与受控状态枚举。

约定(数据模型文档 §2):
- 主键使用 UUID;
- 时间使用 UTC ``timestamptz``;
- JSON 使用 JSONB;
- 受控枚举使用非原生 ENUM(VARCHAR + CHECK),便于部分唯一索引按字面值过滤。
"""

import enum
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全部持久化模型的声明基类。"""


class UUIDPrimaryKeyMixin:
    """UUID 主键,应用侧生成(默认 uuid4)。"""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    """服务端生成的创建时间。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TimestampMixin(CreatedAtMixin):
    """创建与更新时间。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def db_enum(enum_cls: type[enum.Enum], constraint_name: str) -> sa.Enum:
    """构建非原生枚举类型:VARCHAR 存储 + CHECK 约束。

    枚举成员的 name 与 value 一致,数据库按字面值存储,
    部分唯一索引的 WHERE 子句可直接比较字符串。
    """
    return sa.Enum(
        enum_cls,
        name=constraint_name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class AccountStatus(str, enum.Enum):
    """用户 / 工作区 / 成员账号状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ExecutionMode(str, enum.Enum):
    """Workspace 执行模式(身份与权限文档 §4.2、需求文档 §7.4)。"""

    READ_ONLY = "READ_ONLY"
    CONFIRM_WRITES = "CONFIRM_WRITES"
    AUTO_EXECUTE = "AUTO_EXECUTE"


class MembershipRole(str, enum.Enum):
    """受控角色(数据模型文档 §3)。"""

    WORKSPACE_OWNER = "WORKSPACE_OWNER"
    OPERATOR = "OPERATOR"


class ResourceScopeMode(str, enum.Enum):
    """通用资源范围模式。"""

    ALL = "ALL"
    SELECTED = "SELECTED"


class ConfigScope(str, enum.Enum):
    """Skill / Tool 定义作用域。"""

    GLOBAL = "GLOBAL"
    WORKSPACE = "WORKSPACE"


class ToolEffect(str, enum.Enum):
    """Tool 副作用分类。"""

    READ = "READ"
    WRITE = "WRITE"


class ModelBackendType(str, enum.Enum):
    """模型后端类型(数据模型文档 §4.2)。"""

    OPENAI = "OPENAI"
    ANTHROPIC = "ANTHROPIC"


class ConfigVersionStatus(str, enum.Enum):
    """版本化配置状态:发布版本不可原地更新。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class SessionStatus(str, enum.Enum):
    """Session 生命周期状态。"""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MessageRole(str, enum.Enum):
    """Message 类型;SESSION_SUMMARY 是 Runtime 内部类型。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"
    SESSION_SUMMARY = "SESSION_SUMMARY"


class MessageStatus(str, enum.Enum):
    """Assistant Message 状态(数据模型文档 §5)。"""

    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class RunStatus(str, enum.Enum):
    """Run 生命周期状态(执行状态文档 §4.1)。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_RUN_STATUS_VALUES = (
    "QUEUED",
    "RUNNING",
    "WAITING_INPUT",
    "WAITING_CONFIRMATION",
    "WAITING_RECONCILIATION",
)
"""允许占用 Session 活动位的 Run 状态(部分唯一索引过滤条件)。"""


class RunAttemptStatus(str, enum.Enum):
    """RunAttempt 状态(执行状态文档 §3.3)。"""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class ToolCallStatus(str, enum.Enum):
    """ToolCall 状态(执行状态文档 §4.2)。"""

    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class OperationStatus(str, enum.Enum):
    """Operation 逻辑写入状态(数据模型文档 §7)。"""

    PREPARED = "PREPARED"
    EXECUTING = "EXECUTING"
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class OperationAttemptKind(str, enum.Enum):
    """OperationAttempt 类型(数据模型文档 §7)。"""

    EXECUTE = "EXECUTE"
    RECONCILE = "RECONCILE"


class OperationAttemptStatus(str, enum.Enum):
    """OperationAttempt 状态。"""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class RunJobKind(str, enum.Enum):
    """RunJob 类型(数据模型文档 §9)。"""

    ADVANCE_RUN = "ADVANCE_RUN"
    RECONCILE_OPERATION = "RECONCILE_OPERATION"


class RunJobStatus(str, enum.Enum):
    """RunJob 租约状态(执行状态文档 §4.3)。"""

    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    DEAD = "DEAD"


ACTIVE_JOB_STATUS_VALUES = ("PENDING", "LEASED")
"""允许占用 Run 活动 Job 位的 Job 状态(部分唯一索引过滤条件)。"""


class InputRequestStatus(str, enum.Enum):
    """Input Request 状态(执行状态文档 §4.4)。"""

    PENDING = "PENDING"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ConfirmationRequestStatus(str, enum.Enum):
    """Confirmation Request 状态(执行状态文档 §4.4)。"""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class MemoryLifecycleStatus(str, enum.Enum):
    """记忆本地生命周期状态(数据模型文档 §11)。"""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"


class MemorySyncStatus(str, enum.Enum):
    """记忆外部同步状态,与本地生命周期分离(数据模型文档 §11)。"""

    PENDING = "PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


class OutboxKind(str, enum.Enum):
    """集成 Outbox 类型(数据模型文档 §11)。"""

    MEMORY_UPSERT = "MEMORY_UPSERT"
    MEMORY_DELETE = "MEMORY_DELETE"
    TRACE_EXPORT = "TRACE_EXPORT"


class OutboxStatus(str, enum.Enum):
    """集成 Outbox 状态(数据模型文档 §11)。"""

    PENDING = "PENDING"
    LEASED = "LEASED"
    DONE = "DONE"
    DEAD = "DEAD"


__all__ = [
    "ACTIVE_JOB_STATUS_VALUES",
    "ACTIVE_RUN_STATUS_VALUES",
    "AccountStatus",
    "Base",
    "ConfigScope",
    "ConfigVersionStatus",
    "ConfirmationRequestStatus",
    "CreatedAtMixin",
    "ExecutionMode",
    "InputRequestStatus",
    "MembershipRole",
    "MemoryLifecycleStatus",
    "MemorySyncStatus",
    "MessageRole",
    "MessageStatus",
    "ModelBackendType",
    "OperationAttemptKind",
    "OperationAttemptStatus",
    "OperationStatus",
    "OutboxKind",
    "OutboxStatus",
    "ResourceScopeMode",
    "RunAttemptStatus",
    "RunJobKind",
    "RunJobStatus",
    "RunStatus",
    "SessionStatus",
    "TimestampMixin",
    "ToolCallStatus",
    "ToolEffect",
    "UUIDPrimaryKeyMixin",
    "db_enum",
]
