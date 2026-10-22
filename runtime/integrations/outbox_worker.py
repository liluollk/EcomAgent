"""通用集成 Outbox Worker(计划 04 Task 7 Step 3;数据模型文档 §11)。

- ``claim``:``FOR UPDATE SKIP LOCKED`` 短租约领取到期的 ``PENDING`` 行,
  或重新领取租约已过期的 ``LEASED`` 行(失租 Worker 恢复);生成 secrets
  随机 ``lease_token`` 作为 fencing 凭据;
- ``process``:把租约交给注入的 :class:`OutboxHandler`;成功 → ``DONE``;
  失败按 attempt 预算延迟重试(回 ``PENDING`` 并按指数退避推进
  ``available_at``),超过预算置 ``DEAD``;
- 集成失败只改变 Outbox 自身状态,不读取也不修改任何 Run 状态;
- 服务只 flush 不 commit,事务边界归调用方。
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import OutboxKind, OutboxStatus
from runtime.persistence.models.integrations import IntegrationOutbox


DEFAULT_LEASE_DURATION = timedelta(seconds=30)
"""领取的默认短租约时长(Outbox 自己的租约,与 Run 执行无关)。"""

DEFAULT_MAX_ATTEMPTS = 3
"""默认 attempt 预算:超过即置 DEAD。"""

DEFAULT_RETRY_DELAY = timedelta(seconds=5)
"""延迟重试的基础退避;第 n 次失败后延迟 = retry_delay * 2^(n-1)。"""


class OutboxError(RuntimeError):
    """集成处理失败的统一归一;``code`` 进入 Outbox 的 ``last_error_code``。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OutboxEntry:
    """交给 handler 的 Outbox 行视图;``aggregate_id`` 指向权威聚合。"""

    outbox_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: OutboxKind
    aggregate_type: str
    aggregate_id: str
    attempt_count: int


@dataclass(frozen=True)
class OutboxLease:
    """一次成功领取的 Outbox 租约;``lease_token`` 是 fencing 凭据。"""

    outbox_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: OutboxKind
    lease_token: uuid.UUID
    lease_expires_at: datetime
    attempt_count: int


class OutboxHandler(Protocol):
    """按 Outbox kind 处理外部同步的回调协议;失败抛 :class:`OutboxError`。"""

    async def handle(self, entry: OutboxEntry) -> None: ...


def _new_lease_token() -> uuid.UUID:
    """secrets CSPRNG 生成的 UUIDv4 形态 lease token。"""
    return uuid.UUID(bytes=secrets.token_bytes(16), version=4)


class OutboxWorker:
    """integration_outbox 的短租约 Worker;在调用方事务内执行,只 flush。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        handler: OutboxHandler | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay: timedelta = DEFAULT_RETRY_DELAY,
    ) -> None:
        self._session = session
        self._handler = handler
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay

    async def claim(self, worker_id: str) -> OutboxLease | None:
        """领取一条到期 PENDING 或租约过期的 LEASED Outbox;无则 None。"""
        return await self._claim(worker_id, kind=None)

    async def process(self, lease: OutboxLease) -> bool:
        """以租约处理一条 Outbox;成功 DONE 返回 True,失败走重试返回 False。

        fencing:仅当行仍为 LEASED 且 token 匹配才处理;失租 Worker 直接放弃。
        """
        if self._handler is None:
            raise RuntimeError("OutboxWorker.process requires an injected handler")
        row = await self._session.get(IntegrationOutbox, lease.outbox_id)
        if (
            row is None
            or row.status is not OutboxStatus.LEASED
            or row.lease_token != lease.lease_token
        ):
            return False
        entry = OutboxEntry(
            outbox_id=row.id,
            workspace_id=row.workspace_id,
            kind=row.kind,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            attempt_count=row.attempt_count,
        )
        try:
            await self._handler.handle(entry)
        except OutboxError as error:
            await self._fail(lease, error.code)
            return False
        row.status = OutboxStatus.DONE
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = None
        await self._session.flush()
        return True

    async def process_with_failure(
        self,
        kind: str | OutboxKind,
        *,
        error_code: str = "HANDLER_FORCED_FAILURE",
    ) -> bool:
        """测试注入:领取一条指定 kind 的到期 Outbox 并直接走失败路径。"""
        lease = await self._claim(
            "outbox-worker-failure-test", kind=OutboxKind(kind)
        )
        if lease is None:
            return False
        await self._fail(lease, error_code)
        return True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _claim(
        self, worker_id: str, *, kind: OutboxKind | None
    ) -> OutboxLease | None:
        kind_filter = [] if kind is None else [IntegrationOutbox.kind == kind]
        pending_due = sa.and_(
            IntegrationOutbox.status == OutboxStatus.PENDING,
            IntegrationOutbox.available_at <= sa.func.now(),
            *kind_filter,
        )
        lease_expired = sa.and_(
            IntegrationOutbox.status == OutboxStatus.LEASED,
            IntegrationOutbox.lease_expires_at <= sa.func.now(),
            *kind_filter,
        )
        stmt = (
            select(IntegrationOutbox)
            .where(sa.or_(pending_due, lease_expired))
            .order_by(IntegrationOutbox.available_at, IntegrationOutbox.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await self._session.scalars(stmt)).one_or_none()
        if row is None:
            return None
        row.status = OutboxStatus.LEASED
        row.lease_owner = worker_id
        row.lease_token = _new_lease_token()
        row.lease_expires_at = datetime.now(UTC) + self._lease_duration
        row.attempt_count += 1
        await self._session.flush()
        assert row.lease_token is not None
        assert row.lease_expires_at is not None
        return OutboxLease(
            outbox_id=row.id,
            workspace_id=row.workspace_id,
            kind=row.kind,
            lease_token=row.lease_token,
            lease_expires_at=row.lease_expires_at,
            attempt_count=row.attempt_count,
        )

    async def _fail(self, lease: OutboxLease, error_code: str) -> None:
        """结束一次失败的租约:预算内回 PENDING 延迟重试,超预算置 DEAD。"""
        exhausted = lease.attempt_count >= self._max_attempts
        values: dict[str, Any] = {
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "last_error_code": error_code,
        }
        if exhausted:
            values["status"] = OutboxStatus.DEAD
        else:
            values["status"] = OutboxStatus.PENDING
            values["available_at"] = datetime.now(UTC) + self._retry_delay * (
                2 ** (lease.attempt_count - 1)
            )
        stmt = (
            sa.update(IntegrationOutbox)
            .where(
                IntegrationOutbox.id == lease.outbox_id,
                IntegrationOutbox.lease_token == lease.lease_token,
                IntegrationOutbox.status == OutboxStatus.LEASED,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        cast("CursorResult[Any]", await self._session.execute(stmt))


__all__ = [
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_DELAY",
    "OutboxEntry",
    "OutboxError",
    "OutboxHandler",
    "OutboxLease",
    "OutboxWorker",
]
