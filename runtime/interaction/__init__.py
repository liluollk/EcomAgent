"""Runtime 交互服务:Input/Confirmation Request 的持久化事务(计划 02 Task 7)。

本包实现 durable Input Request 与 Confirmation Request 的应用服务:
- 字段白名单与单次消费的回答(:class:`InputService`);
- 规范化指纹与 TOCTOU 防护(:class:`ConfirmationService`);
- 过期请求的批量清扫(:class:`ExpireService`)。

所有状态迁移经 :class:`~runtime.audit.service.AuditService` 写只追加审计,
与调用方共享事务(只 flush 不 commit)。
"""

from runtime.interaction.confirmation_service import (
    CONFIRMATION_ACCEPTED,
    CONFIRMATION_CANCELLED,
    CONFIRMATION_CREATED,
    CONFIRMATION_DECISION_REJECTED,
    CONFIRMATION_EXPIRED,
    CONFIRMATION_NOT_ACCEPTED,
    CONFIRMATION_REJECTED,
    CONFIRMATION_STALE,
    REQUEST_EXPIRED,
    REQUEST_NOT_FOUND,
    REQUEST_NOT_PENDING,
    ConfirmationRequestError,
    ConfirmationService,
    VerifyResult,
)
from runtime.interaction.expiry import ExpireService
from runtime.interaction.fingerprints import (
    ConfirmationBinding,
    canonical_json,
    confirmation_fingerprint,
    normalized_arguments_digest,
)
from runtime.interaction.input_service import (
    INPUT_ANSWER_REJECTED,
    INPUT_ANSWERED,
    INPUT_REQUEST_CANCELLED,
    INPUT_REQUEST_CREATED,
    INPUT_REQUEST_EXPIRED,
    UNDECLARED_FIELD,
    InputAnswer,
    InputAnswerRejected,
    InputRequestError,
    InputService,
)


__all__ = [
    "CONFIRMATION_ACCEPTED",
    "CONFIRMATION_CANCELLED",
    "CONFIRMATION_CREATED",
    "CONFIRMATION_DECISION_REJECTED",
    "CONFIRMATION_EXPIRED",
    "CONFIRMATION_NOT_ACCEPTED",
    "CONFIRMATION_REJECTED",
    "CONFIRMATION_STALE",
    "INPUT_ANSWERED",
    "INPUT_ANSWER_REJECTED",
    "INPUT_REQUEST_CANCELLED",
    "INPUT_REQUEST_CREATED",
    "INPUT_REQUEST_EXPIRED",
    "REQUEST_EXPIRED",
    "REQUEST_NOT_FOUND",
    "REQUEST_NOT_PENDING",
    "UNDECLARED_FIELD",
    "ConfirmationBinding",
    "ConfirmationRequestError",
    "ConfirmationService",
    "ExpireService",
    "InputAnswer",
    "InputAnswerRejected",
    "InputRequestError",
    "InputService",
    "VerifyResult",
    "canonical_json",
    "confirmation_fingerprint",
    "normalized_arguments_digest",
]
