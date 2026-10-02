"""事务所有权:UnitOfWork Protocol 与 SQLAlchemy 实现(计划 01 Task 4、架构文档 §13)。

语义:
- ``__aenter__`` 创建 ``AsyncSession`` 并装配六个 Repository;
- 正常退出**不自动提交**;异常退出执行 rollback 并关闭 Session;
- ``commit()``/``rollback()`` 为显式方法,应用服务拥有事务边界;
- Repository 只调用 flush,绝不调用 commit。
"""

from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.persistence.repositories.events import EventRepository
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.requests import RequestRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository
from runtime.persistence.repositories.workspaces import WorkspaceRepository


class UnitOfWork(Protocol):
    """应用服务通过 UoW 获得共享同一事务的六个 Repository。"""

    workspaces: WorkspaceRepository
    sessions: SessionRepository
    runs: RunRepository
    jobs: JobRepository
    requests: RequestRepository
    events: EventRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork(UnitOfWork):
    """SQLAlchemy/asyncpg 实现;Repository 在 ``__aenter__`` 时绑定 Session。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = AsyncSession(self._engine, expire_on_commit=False)
        session = self._session
        self.workspaces = WorkspaceRepository(session)
        self.sessions = SessionRepository(session)
        self.runs = RunRepository(session)
        self.jobs = JobRepository(session)
        self.requests = RequestRepository(session)
        self.events = EventRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            # 正常退出不自动提交;close() 丢弃未提交事务。异常退出显式回滚。
            if exc_type is not None:
                await session.rollback()
        finally:
            await session.close()

    async def commit(self) -> None:
        await self._require_session().commit()

    async def rollback(self) -> None:
        await self._require_session().rollback()

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError(
                "UnitOfWork 未进入上下文或已退出;请在 async with 块内调用"
            )
        return self._session
