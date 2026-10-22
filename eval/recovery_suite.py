"""故障恢复 Eval 套件(计划 05 Task 6)。

数据声明期望,代码执行验证:``eval/datasets/recovery/faults.yaml`` 声明七类
故障用例的 ``id/category/risk_level/fault``(故障注名,执行分派键)与权威
期望(回查路径、逻辑写上界、最终状态);执行逻辑在 :class:`RecoverySuite`
内按故障注名分派到确定性的执行函数——每个用例经
:class:`eval.fixtures.IsolatedEnvironment` 种子独立权威环境(身份 + 业务
状态 + 完整商品应用环境),按故障注名构造执行路径:复用 runtime 真实服务
(:class:`~runtime.execution.executor.RunExecutor`、
:class:`~runtime.execution.operations.OperationService`、
:class:`~runtime.execution.reconciliation.ReconciliationService`、
``adapters.commerce.faults.FaultInjector``、确定性 ScriptedBackend)与
tests/runtime 集成测试的 crash-harness 模式(领取 → 进程中断 → 恢复扫描 →
继续推进)。每例断言权威最终状态与逻辑写数后报告该例通过与否。

全部用例确定性:无网络、无真实模型、无随机(行标识用 uuid,不参与断言),
故障只由 ``FaultInjector.inject``/crash-harness 显式控制,重复运行产生相同
结论。

结果模型(:class:`RecoverySuiteReport` / :class:`RecoveryCaseOutcome`)与
:mod:`eval.runtime_suite` 同语义:``failed``/``infrastructure_errors``/
``skipped``/``passed`` 计数;基础设施失败(连接/依赖不可用)与断言失败
(权威不变式被破坏)分开报告,不猜测。恢复指标定义:

- ``recovery_success_rate``:通过用例数 / 总用例数;
- ``actual_duplicate_writes``:Σ max(0, 观测逻辑写数 − 声明写上界)——
  平台侧确认发生的逻辑写超出声明的部分(与 :mod:`eval.scorers.recovery`
  的 ``duplicate_logical_writes`` 同语义);
- ``final_state_consistency``:权威最终状态(平台价格 + Run 终态)与声明
  一致的用例占比;
- ``unknown_handling_correct``:观测回查路径与声明
  ``expected_reconcile_path`` 一致的用例占比(未声明路径的用例要求未发生
  UNKNOWN,禁止盲目回查语义漂移)。

CLI::

    python -m eval.recovery_suite --dataset eval/datasets/recovery/faults.yaml

期望输出首行 ``actual_duplicate_writes=0 failed=0 infrastructure_errors=0
skipped=0`` 且退出码 0。

本模块禁止 import apps(与 eval 包其余部分同一约束)。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa
import yaml
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from adapters.commerce.faults import (
    PERMANENT_FAILURE,
    RESPONSE_LOST_AFTER_COMMIT,
    TEMPORARY_RECONCILE_UNKNOWN,
)
from agents.product_operations.persistence.models import Listing
from agents.product_operations.pricing.gateway import SetListingPriceCommand
from composition.product_operations import ProductApplication
from eval.fixtures import IsolatedEnvironment
from eval.schema import SCHEMA_VERSION, Scenario
from runtime.backend.protocol import (
    AgentBackend,
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendFailed,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)
from runtime.execution.executor import RecoveryReport, RunExecutor
from runtime.execution.run_service import RunService
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    InputRequestStatus,
    RunAttemptStatus,
)
from runtime.persistence.models.events import AgentEvent
from runtime.persistence.models.execution import (
    Operation,
    Run,
    RunAttempt,
    ToolCall,
)
from runtime.persistence.models.interaction import ConfirmationRequest, InputRequest
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


RECOVERY_DATASET_PATH: Final[Path] = (
    Path(__file__).resolve().parent / "datasets" / "recovery" / "faults.yaml"
)

REQUIRED_CASE_IDS: Final[frozenset[str]] = frozenset(
    {
        "model-timeout",
        "executor-exit-before-write",
        "write-applied-response-lost",
        "executor-restart-after-confirmation",
        "platform-persistent-failure",
        "reconcile-temporarily-inconsistent",
        "duplicate-submit",
    }
)

REQUIRED_FAULTS: Final[frozenset[str]] = frozenset(
    {
        "model_timeout",
        "executor_exit_before_write",
        "response_lost_after_commit",
        "executor_restart_after_confirmation",
        "permanent_failure",
        "temporary_reconcile_unknown",
        "duplicate_submit",
    }
)

FaultName = Literal[
    "model_timeout",
    "executor_exit_before_write",
    "response_lost_after_commit",
    "executor_restart_after_confirmation",
    "permanent_failure",
    "temporary_reconcile_unknown",
    "duplicate_submit",
]

ReconcileStep = Literal["UNKNOWN", "APPLIED", "NOT_APPLIED"]

_MODEL_TIMEOUT_CODE: Final = "MODEL_TIMEOUT"

_INITIAL_PRICE: Final = "128.00"
_TARGET_PRICE: Final = "99.00"
_USER_MESSAGE: Final = "把 SKU-001 改成 99 元"

_RUN_RESUMED_EVENT: Final = "RUN_RESUMED"
_RUN_WAITING_RECONCILIATION: Final = "RUN_WAITING_RECONCILIATION"
_TOOL_RESULT_RECORDED: Final = "TOOL_RESULT_RECORDED"
_OPERATION_RECONCILED: Final = "OPERATION_RECONCILED"

_MAX_DRIVE_STEPS: Final = 8
"""单用例 input/confirm 恢复推进步数的确定性上界。"""


class CaseFailure(RuntimeError):
    """用例断言失败:权威不变式被破坏(与基础设施失败区分)。"""


# ---------------------------------------------------------------------------
# 数据集声明:YAML → RecoveryCase(数据声明期望,代码执行验证)
# ---------------------------------------------------------------------------


class FinalStateExpectation(BaseModel):
    """权威最终状态期望:Mock 平台价格 + Run 终态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    price: str = Field(min_length=1)
    run_status: str = Field(min_length=1)


class RecoveryCase(BaseModel):
    """数据集声明的故障恢复用例:身份、风险分级与权威期望,不含执行逻辑。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"]
    fault: FaultName
    expectation: str = Field(min_length=1)
    expected_reconcile_path: list[ReconcileStep] = Field(default_factory=list)
    max_logical_writes: int = Field(default=0, ge=0)
    expected_final_state: FinalStateExpectation


def load_recovery_dataset(source: Path | str) -> list[RecoveryCase]:
    """加载数据集文件或目录(按文件名排序);声明与执行代码漂移即拒绝。"""
    target = Path(source)
    if target.is_dir():
        cases: list[RecoveryCase] = []
        for file in sorted(target.iterdir()):
            if file.is_file() and file.suffix.lower() in (".yaml", ".yml"):
                cases.extend(load_recovery_dataset(file))
        return cases
    if target.suffix.lower() not in (".yaml", ".yml"):
        raise ValueError(f"unsupported dataset file suffix {target.suffix!r}: {target}")
    document: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"recovery dataset {target} top level must be a mapping")
    declared = document.get("schema_version")
    if declared != SCHEMA_VERSION:
        raise ValueError(
            f"recovery dataset {target} declares schema_version={declared!r}, "
            f"expected {SCHEMA_VERSION}"
        )
    items = document.get("cases")
    if not isinstance(items, list):
        raise TypeError(f"recovery dataset {target} must declare a list of cases")
    cases = [RecoveryCase.model_validate(item) for item in items]
    for case in cases:
        if case.fault not in _FAULT_EXECUTORS:
            raise ValueError(
                f"recovery dataset {target} declares fault {case.fault!r} "
                "(case " + case.id + ") with no deterministic executor"
            )
    return cases


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


CaseStatus = Literal["PASSED", "FAILED", "INFRASTRUCTURE_ERROR", "SKIPPED"]


class RecoveryCaseOutcome(BaseModel):
    """单个故障恢复用例的结果(声明期望 + 观测事实,frozen 可序列化)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    risk_level: str
    fault: str
    expectation: str
    status: CaseStatus
    passed: bool
    included_in_quality_score: bool
    error: str | None = None

    expected_reconcile_path: list[str] = Field(default_factory=list)
    max_logical_writes: int = 0
    expected_final_state: FinalStateExpectation

    observed_reconcile_path: list[str] = Field(default_factory=list)
    observed_logical_writes: int = 0
    observed_price: str | None = None
    observed_run_status: str | None = None
    path_matches: bool = False
    final_state_matches: bool = False


class RecoverySuiteReport(BaseModel):
    """一轮故障恢复套件运行的全部结果与恢复指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    results: tuple[RecoveryCaseOutcome, ...] = ()

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.status == "PASSED")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status == "FAILED")

    @property
    def infrastructure_errors(self) -> int:
        return sum(
            1 for result in self.results if result.status == "INFRASTRUCTURE_ERROR"
        )

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.status == "SKIPPED")

    @property
    def recovery_success_rate(self) -> float:
        """通过用例数 / 总用例数(空套件为 0.0)。"""
        return self.passed / self.total if self.total else 0.0

    @property
    def actual_duplicate_writes(self) -> int:
        """Σ max(0, 观测逻辑写数 − 声明写上界):重复副作用总量。"""
        return sum(
            max(0, result.observed_logical_writes - result.max_logical_writes)
            for result in self.results
        )

    @property
    def final_state_consistency(self) -> float:
        """权威最终状态(价格 + Run 终态)与声明一致的用例占比。"""
        if not self.total:
            return 0.0
        matching = sum(1 for result in self.results if result.final_state_matches)
        return matching / self.total

    @property
    def unknown_handling_correct(self) -> float:
        """观测回查路径与声明一致的用例占比(含"未声明即不得出现 UNKNOWN")。"""
        if not self.total:
            return 0.0
        matching = sum(1 for result in self.results if result.path_matches)
        return matching / self.total

    def summary_line(self) -> str:
        """计划要求的发布输出行。"""
        return (
            f"actual_duplicate_writes={self.actual_duplicate_writes} "
            f"failed={self.failed} "
            f"infrastructure_errors={self.infrastructure_errors} skipped={self.skipped}"
        )


# ---------------------------------------------------------------------------
# 确定性替身:ScriptedBackend 注入(外部世界 = 模型流)
# ---------------------------------------------------------------------------


class _PriceWriteBackend:
    """确定性脚本模型:首个模型轮发 set_listing_price(缺店铺),其后一律收尾。

    与 apps/scripted_e2e.py 的脚本同形(eval 不 import apps)。"首个模型轮"
    由后端状态计数:恢复推进(确认/回查后的重入)不重新提出 ToolCall,
    写结果回填后的模型轮回固定完成文本,Run 随之 COMPLETED。
    """

    def __init__(self, *, sku: str = "SKU-001", target_price: str = "99.00") -> None:
        self._sku = sku
        self._target_price = target_price
        self._proposed = False

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        del request, cancellation  # 脚本无长阻塞点,不需要中途取消。
        if self._proposed:
            yield TextDelta(text_delta="改价已执行完成。")
            yield BackendCompleted(finish_reason="stop")
            return
        self._proposed = True
        yield ToolCallCompleted(
            call_id=f"recovery-{uuid.uuid4().hex[:12]}",
            name="set_listing_price",
            arguments={"sku": self._sku, "target_price": self._target_price},
        )
        yield BackendCompleted(finish_reason="stop")


class _ModelTimeoutBackend:
    """模型超时注入:前 N 轮流回放可重试 MODEL_TIMEOUT 的归一化 Provider 失败。

    重试预算(backend_retries=1)耗尽后流以 MODEL_TIMEOUT 终止:Run 确定性
    FAILED,无任何 ToolCall/副作用。超时在真实 Backend 中被归一化为
    ``BackendFailed`` 事件——本类即注入该真实语义。
    """

    def __init__(self, *, failures: int = 2) -> None:
        self._remaining = failures

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        del request, cancellation
        if self._remaining > 0:
            self._remaining -= 1
            yield BackendFailed(error_code=_MODEL_TIMEOUT_CODE, retryable=True)
            return
        yield TextDelta(text_delta="不应到达的收尾文本。")
        yield BackendCompleted(finish_reason="stop")


# ---------------------------------------------------------------------------
# 共享:权威环境与观测视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CaseObservation:
    """从真库与平台侧回读的用例观测(供套件与声明期望对账)。"""

    run_status: str
    price: str
    logical_writes: int
    reconcile_path: tuple[str, ...]


@dataclass(frozen=True)
class _PendingConfirmation:
    """推进到确认边界的 Run 标识(确认接受/重启用例的接续锚点)。"""

    run_id: uuid.UUID
    confirmation_id: uuid.UUID


def _scenario(case_id: str) -> Scenario:
    """种子 128.00 的 Listing;目标价 99.00(合法变更,经确认执行)。"""
    return Scenario.model_validate(
        {
            "id": case_id,
            "category": "fault_recovery",
            "risk_level": "high",
            "initial_state": {
                "listings": [
                    {
                        "store_id": "store-1",
                        "sku": "SKU-001",
                        "price": _INITIAL_PRICE,
                        "currency": "CNY",
                        "status": "PUBLISHED",
                    }
                ]
            },
            "workspace": {"name": "recovery-suite"},
            "actor": {
                "role": "owner",
                "permissions": ["product.read", "product.price.write"],
                "store_scope_mode": "ALL",
            },
            "messages": [{"role": "user", "content": _USER_MESSAGE}],
            "expected_final_state": {
                "listings": [
                    {"store_id": "store-1", "sku": "SKU-001", "price": _TARGET_PRICE}
                ],
                "max_logical_writes": 1,
            },
            "allowed_side_effects": [
                {"tool": "set_listing_price", "target": "store-1/SKU-001"},
            ],
        }
    )


@asynccontextmanager
async def _isolated_environment(
    engine: AsyncEngine, case_id: str, backend: AgentBackend
) -> AsyncIterator[IsolatedEnvironment]:
    """每用例独立权威环境;teardown 清理防止残留 Job 被后续用例误领取。"""
    environment = await IsolatedEnvironment.create(
        engine, _scenario(case_id), backend=backend
    )
    try:
        yield environment
    finally:
        await environment.aclose()
        await environment.cleanup()


async def _drive_to_confirmation(
    environment: IsolatedEnvironment,
) -> _PendingConfirmation:
    """启动 Run 并驱动到确认边界(缺店铺 Input Request 按种子符号回答)。"""
    application: ProductApplication = environment.application
    view = await application.start(_USER_MESSAGE)
    for _ in range(_MAX_DRIVE_STEPS):
        if view.status is RunStatus.WAITING_INPUT:
            if view.pending_input_id is None:
                raise CaseFailure("WAITING_INPUT without a pending input request")
            values = await _input_values(environment, view.pending_input_id)
            view = await application.answer(view.pending_input_id, values)
        elif view.status is RunStatus.WAITING_CONFIRMATION:
            if view.pending_confirmation_id is None:
                raise CaseFailure("WAITING_CONFIRMATION without a pending request")
            return _PendingConfirmation(
                run_id=view.run_id, confirmation_id=view.pending_confirmation_id
            )
        else:
            raise CaseFailure(f"unexpected drive status {view.status.value}")
    raise CaseFailure("drive to confirmation exceeded the step budget")


async def _input_values(
    environment: IsolatedEnvironment, request_id: uuid.UUID
) -> dict[str, str]:
    """为缺店铺 Input Request 构造确定性回答(店铺经种子符号映射)。"""
    session = AsyncSession(environment.engine, expire_on_commit=False)
    try:
        request = (
            await session.scalars(
                select(InputRequest).where(
                    InputRequest.workspace_id == environment.workspace_id,
                    InputRequest.id == request_id,
                )
            )
        ).one_or_none()
        if request is None:
            raise CaseFailure(f"input request {request_id} vanished")
        sku: str | None = None
        if request.tool_call_id is not None:
            tool_call = (
                await session.scalars(
                    select(ToolCall).where(ToolCall.id == request.tool_call_id)
                )
            ).one_or_none()
            if tool_call is not None:
                merged = (
                    tool_call.normalized_arguments
                    if tool_call.normalized_arguments is not None
                    else tool_call.raw_arguments
                )
                raw_sku = dict(merged or {}).get("sku")
                sku = str(raw_sku) if raw_sku is not None else None
        field_paths = list(request.field_paths)
    finally:
        await session.close()
    for field_path in field_paths:
        if field_path != "store_ref":
            raise CaseFailure(f"unsupported input field {field_path!r}")
    store_id = environment.store_id_for_sku(sku) if sku else environment.single_store_id
    if store_id is None:
        raise CaseFailure("no seeded store to answer the pending input request")
    symbol = environment.store_symbol_for_id(str(store_id))
    if symbol is None:
        raise CaseFailure("seeded store has no scenario symbol")
    return {"store_ref": symbol}


async def _accept_confirmation(
    engine: AsyncEngine, environment: IsolatedEnvironment, pending: _PendingConfirmation
) -> None:
    """确认接受并重新排队(与 ProductApplication.confirm 同一事务语义,但不推进)。

    这是"确认后进程重启"的故障窗口:接受已提交、Run 已回 QUEUED,尚无执行器
    领取。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await ConfirmationService(
                session,
                workspace_id=environment.workspace_id,
                actor_id=environment.actor_id,
            ).accept(pending.confirmation_id)
            service = RunService(session=session, workspace_id=environment.workspace_id)
            await service.transition_run(
                run_id=pending.run_id,
                target_status=RunStatus.QUEUED,
                event_kind=_RUN_RESUMED_EVENT,
                event_payload={"confirmation_id": str(pending.confirmation_id)},
                expected_status=RunStatus.WAITING_CONFIRMATION,
            )
    finally:
        await session.close()


async def _seed_run(engine: AsyncEngine, environment: IsolatedEnvironment) -> uuid.UUID:
    """crash-harness 种子:经真实仓储创建 Session 与 Run(QUEUED)。"""
    capability = dict(environment.application.capability_ids)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            session_row = await SessionRepository(session).create(
                workspace_id=environment.workspace_id,
                agent_id=uuid.UUID(str(capability["agent_id"])),
                agent_version_id=uuid.UUID(str(capability["agent_version_id"])),
                created_by_actor_id=environment.actor_id,
                created_by_membership_id=environment.membership_id,
            )
            run = await RunRepository(session).create(
                workspace_id=environment.workspace_id,
                session_id=session_row.id,
                created_by_actor_id=environment.actor_id,
                created_by_membership_id=environment.membership_id,
                agent_version_id=uuid.UUID(str(capability["agent_version_id"])),
                model_config_version_id=uuid.UUID(
                    str(capability["model_config_version_id"])
                ),
            )
            return run.id
    finally:
        await session.close()


class _RestartDriver:
    """重启后的推进驱动:从持久化重建"已回答字段"视图。

    进程重启后门面内存里的回答视图为空;若直接推进,模型会重复追问已经回答
    过的字段。这里在每次 advance 前从 InputRequest/ToolCall 重建,与 API
    宿主的真实重启路径同语义。
    """

    def __init__(self, environment: IsolatedEnvironment) -> None:
        self._environment = environment

    async def advance(self, claim: Any, *, answered: Mapping[str, str] | None = None) -> Any:
        del answered
        rebuilt = await _answered_view(
            self._environment.engine, self._environment, claim.run_id
        )
        return await self._environment.application.advance(
            claim, answered=rebuilt or None
        )

    async def reconcile(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        return await self._environment.application.reconcile(run_id, workspace_id)


def _executor(
    environment: IsolatedEnvironment, *, executor_id: str
) -> RunExecutor:
    """构造一个不取锁、不起后台循环的执行器(评测内手动驱动恢复扫描)。"""
    return RunExecutor(
        session_factory=lambda: AsyncSession(environment.engine, expire_on_commit=False),
        driver=_RestartDriver(environment),
        instance_id=executor_id,
        lock=None,
    )


async def _recover(
    environment: IsolatedEnvironment, *, executor_id: str
) -> RecoveryReport:
    """跑一次启动恢复扫描(进程重启后的唯一入口)。"""
    return await _executor(environment, executor_id=executor_id).recover()


async def _interrupt_run(
    engine: AsyncEngine,
    environment: IsolatedEnvironment,
    run_id: uuid.UUID,
    *,
    executor_id: str,
) -> uuid.UUID:
    """模拟"执行器已领取并开始推进、随后进程崩溃"。

    对应 :meth:`RunExecutor._claim`:CAS QUEUED → RUNNING 并创建 RUNNING
    Attempt。此后不再收尾——Run 与 Attempt 都停在 RUNNING,正是崩溃后的库内
    形态。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            run = (
                await session.scalars(
                    select(Run)
                    .where(
                        Run.workspace_id == environment.workspace_id,
                        Run.id == run_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).one_or_none()
            if run is None or run.status.value != RunStatus.QUEUED.value:
                raise CaseFailure(
                    "the crash harness can only interrupt a QUEUED run, got "
                    f"{None if run is None else run.status.value}"
                )
            moved = await RunRepository(session).compare_and_set_status(
                workspace_id=environment.workspace_id,
                run_id=run_id,
                expected_version=run.state_version,
                new_status=RunStatus.RUNNING.value,
                expected_status=RunStatus.QUEUED.value,
            )
            if not moved:
                raise CaseFailure("the crash harness lost the CAS race")
            attempt_no = (
                await session.scalar(
                    sa.select(sa.func.max(RunAttempt.attempt_no)).where(
                        RunAttempt.run_id == run_id
                    )
                )
                or 0
            ) + 1
            attempt = RunAttempt(
                workspace_id=environment.workspace_id,
                run_id=run_id,
                attempt_no=attempt_no,
                executor_id=executor_id,
                status=RunAttemptStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
            session.add(attempt)
            await session.flush()
            return attempt.id
    finally:
        await session.close()


async def _answered_view(
    engine: AsyncEngine, environment: IsolatedEnvironment, run_id: uuid.UUID
) -> dict[str, str]:
    """从持久化状态重建 Run 的"已回答字段"视图(与 Worker 重建语义一致)。

    InputService 回答时把值合并进 ToolCall 的 ``normalized_arguments``,
    InputRequest 行本身不存值;因此对每条 ANSWERED InputRequest,从其
    ToolCall 挂起参数按 ``field_paths`` 白名单取回回答值。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    answered: dict[str, str] = {}
    try:
        requests = (
            await session.scalars(
                select(InputRequest).where(
                    InputRequest.workspace_id == environment.workspace_id,
                    InputRequest.run_id == run_id,
                    InputRequest.status == InputRequestStatus.ANSWERED,
                )
            )
        ).all()
        for request in requests:
            if request.tool_call_id is None:
                continue
            tool_call = (
                await session.scalars(
                    select(ToolCall).where(ToolCall.id == request.tool_call_id)
                )
            ).one_or_none()
            if tool_call is None:
                continue
            merged = (
                tool_call.normalized_arguments
                if tool_call.normalized_arguments is not None
                else tool_call.raw_arguments
            )
            if not merged:
                continue
            for field in request.field_paths:
                value = dict(merged).get(field)
                if isinstance(value, str):
                    answered[field] = value
    finally:
        await session.close()
    return answered


# ---------------------------------------------------------------------------
# 共享只读查询(权威事实从真库与平台侧回读)
# ---------------------------------------------------------------------------


async def _read_run(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> Run:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                select(Run)
                .where(Run.workspace_id == workspace_id, Run.id == run_id)
                .execution_options(populate_existing=True)
            )
        ).one()
    finally:
        await session.close()


async def _latest_attempt(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> RunAttempt | None:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                sa.select(RunAttempt)
                .where(RunAttempt.workspace_id == workspace_id, RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
            )
        ).one_or_none()
    finally:
        await session.close()


async def _single_tool_call(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> ToolCall:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        rows = (
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.workspace_id == workspace_id, ToolCall.run_id == run_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    finally:
        await session.close()
    if len(rows) != 1:
        raise CaseFailure(f"expected exactly one ToolCall for the run, found {len(rows)}")
    return rows[0]


async def _single_operation(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> Operation | None:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                select(Operation)
                .join(ToolCall, Operation.tool_call_id == ToolCall.id)
                .where(Operation.workspace_id == workspace_id, ToolCall.run_id == run_id)
                .execution_options(populate_existing=True)
                .limit(1)
            )
        ).one_or_none()
    finally:
        await session.close()


async def _count_tool_calls(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> int:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        count = await session.scalar(
            select(sa.func.count())
            .select_from(ToolCall)
            .where(ToolCall.workspace_id == workspace_id, ToolCall.run_id == run_id)
        )
    finally:
        await session.close()
    return int(count) if count is not None else 0


async def _count_operations(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> int:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        count = await session.scalar(
            select(sa.func.count())
            .select_from(Operation)
            .join(ToolCall, Operation.tool_call_id == ToolCall.id)
            .where(Operation.workspace_id == workspace_id, ToolCall.run_id == run_id)
        )
    finally:
        await session.close()
    return int(count) if count is not None else 0


async def _operation_ids(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> Sequence[uuid.UUID]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                select(Operation.operation_id)
                .join(ToolCall, Operation.tool_call_id == ToolCall.id)
                .where(Operation.workspace_id == workspace_id, ToolCall.run_id == run_id)
            )
        ).all()
    finally:
        await session.close()


async def _logical_writes_of(
    environment: IsolatedEnvironment, operation_ids: Sequence[uuid.UUID]
) -> int:
    """权威逻辑写:平台侧按 operation_id 的真实写计数之和(幂等回放不重复计)。"""
    total = 0
    for operation_id in operation_ids:
        total += await environment.commerce_write_observer.count_logical_writes(
            str(operation_id)
        )
    return total


async def _logical_writes(
    environment: IsolatedEnvironment, run_id: uuid.UUID
) -> int:
    operation_ids = await _operation_ids(environment.engine, environment.workspace_id, run_id)
    return await _logical_writes_of(environment, operation_ids)


async def _authoritative_price(environment: IsolatedEnvironment) -> str:
    """Mock 平台权威价格(种子的唯一个 Listing,两位小数)。"""
    seeded = environment.listings[0]
    session = AsyncSession(environment.engine, expire_on_commit=False)
    try:
        listing = (
            await session.scalars(
                select(Listing).where(
                    Listing.workspace_id == environment.workspace_id,
                    Listing.id == seeded.listing_id,
                )
            )
        ).one()
    finally:
        await session.close()
    return f"{listing.current_price:.2f}"


async def _reconcile_path(
    engine: AsyncEngine, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[str, ...]:
    """从事件流提取 UNKNOWN 后的权威回查路径(与 eval.scorers.recovery 同规则)。

    UNKNOWN 判定:``TOOL_RESULT_RECORDED`` 且 ``outcome=="UNKNOWN"``,或
    ``RUN_WAITING_RECONCILIATION``(UNKNOWN 结果经 Run 迁移事件落库);
    其后每条 ``OPERATION_RECONCILED`` 的 ``payload["result"]`` 依次为路径步。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        rows = (
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.workspace_id == workspace_id, AgentEvent.run_id == run_id)
                .order_by(AgentEvent.sequence_no)
            )
        ).all()
    finally:
        await session.close()
    path: list[str] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        is_unknown_marker = payload.get("tool_call_id") is not None and (
            (row.event_type == _TOOL_RESULT_RECORDED and payload.get("outcome") == "UNKNOWN")
            or row.event_type == _RUN_WAITING_RECONCILIATION
        )
        if is_unknown_marker:
            if not path:
                path.append("UNKNOWN")
        elif row.event_type == _OPERATION_RECONCILED:
            conclusion = payload.get("result")
            if isinstance(conclusion, str):
                path.append(conclusion)
    return tuple(path)


async def _assert_completed_write(
    environment: IsolatedEnvironment,
    run_id: uuid.UUID,
    *,
    expected_path: tuple[str, ...],
) -> _CaseObservation:
    """写恢复用例的共用终态断言:APPLIED/COMPLETED/恰一次写/回查路径。"""
    engine = environment.engine
    workspace_id = environment.workspace_id
    operation = await _single_operation(engine, workspace_id, run_id)
    if operation is None or operation.status.value != "APPLIED":
        raise CaseFailure(
            "operation must record APPLIED after reconciliation, got "
            f"{None if operation is None else operation.status.value}"
        )
    tool_call = await _single_tool_call(engine, workspace_id, run_id)
    if tool_call.status.value != "SUCCEEDED":
        raise CaseFailure(
            f"tool call must succeed after APPLIED reconciliation, got {tool_call.status.value}"
        )
    if await _count_operations(engine, workspace_id, run_id) != 1:
        raise CaseFailure("exactly one authoritative operation is allowed")
    writes = await _logical_writes(environment, run_id)
    if writes != 1:
        raise CaseFailure(f"exactly one external write is allowed, got {writes}")
    price = await _authoritative_price(environment)
    if price != _TARGET_PRICE:
        raise CaseFailure(f"authoritative price must be {_TARGET_PRICE}, got {price}")
    path = await _reconcile_path(engine, workspace_id, run_id)
    if path != expected_path:
        raise CaseFailure(
            f"reconcile path {list(path)} != expected {list(expected_path)}"
        )
    return _CaseObservation(
        run_status="COMPLETED", price=price, logical_writes=writes, reconcile_path=path
    )


# ---------------------------------------------------------------------------
# 七类故障的确定性执行(全部真实服务)
# ---------------------------------------------------------------------------


async def _case_model_timeout(engine: AsyncEngine) -> _CaseObservation:
    """a) model-timeout:模型超时重试预算耗尽 → Run FAILED,零副作用。"""
    async with _isolated_environment(engine, "model-timeout", _ModelTimeoutBackend()) as env:
        view = await env.application.start(_USER_MESSAGE)
        run_id = view.run_id
        run = await _read_run(engine, env.workspace_id, run_id)
        if run.status.value != "FAILED":
            raise CaseFailure(f"run expected FAILED, got {run.status.value}")
        if run.failure_code != _MODEL_TIMEOUT_CODE:
            raise CaseFailure(
                f"failure code expected {_MODEL_TIMEOUT_CODE!r}, got {run.failure_code!r}"
            )
        attempt = await _latest_attempt(engine, env.workspace_id, run_id)
        if attempt is None or attempt.status is not RunAttemptStatus.FAILED:
            raise CaseFailure(
                "a deterministic model timeout must finish the attempt as FAILED, got "
                f"{None if attempt is None else attempt.status.value}"
            )
        if await _count_tool_calls(engine, env.workspace_id, run_id) != 0:
            raise CaseFailure("no ToolCall may be persisted for a model timeout")
        if await _count_operations(engine, env.workspace_id, run_id) != 0:
            raise CaseFailure("no Operation may be registered for a model timeout")
        writes = await _logical_writes(env, run_id)
        if writes != 0:
            raise CaseFailure(f"model timeout must not write, got {writes} writes")
        price = await _authoritative_price(env)
        if price != _INITIAL_PRICE:
            raise CaseFailure(
                f"authoritative price must stay {_INITIAL_PRICE}, got {price}"
            )
        return _CaseObservation(
            run_status=run.status.value, price=price, logical_writes=writes, reconcile_path=()
        )


async def _case_executor_exit_before_write(engine: AsyncEngine) -> _CaseObservation:
    """b) executor-exit-before-write:外部写前执行器崩溃 → 重启扫描接管并续推。

    崩溃现场:Run 与 Attempt 都停在 RUNNING,尚无 ToolCall / Operation / 外部
    写。重启扫描必须收尾遗留 Attempt(ABANDONED + EXECUTOR_RESTART)、把 Run
    退回 QUEUED 并重新推进到下一个用户边界——恢复推进不得凭空跳过仍需用户
    补充的输入,也不得产生任何外部写。
    """
    async with _isolated_environment(engine, "executor-exit-before-write", _PriceWriteBackend()) as env:
        run_id = await _seed_run(engine, env)
        attempt_id = await _interrupt_run(
            engine, env, run_id, executor_id="recovery-lost-executor"
        )
        run = await _read_run(engine, env.workspace_id, run_id)
        if run.status.value != "RUNNING":
            raise CaseFailure(f"a crashed advance must stay RUNNING, got {run.status.value}")
        attempt = await _latest_attempt(engine, env.workspace_id, run_id)
        if attempt is None or attempt.id != attempt_id:
            raise CaseFailure("the interrupted attempt must be the latest attempt")
        if await _count_tool_calls(engine, env.workspace_id, run_id) != 0:
            raise CaseFailure("no ToolCall may exist before the write")
        if await _count_operations(engine, env.workspace_id, run_id) != 0:
            raise CaseFailure("no Operation may exist before the write")
        writes = await _logical_writes(env, run_id)
        if writes != 0:
            raise CaseFailure(f"executor exit before write must not write, got {writes}")
        price = await _authoritative_price(env)
        if price != _INITIAL_PRICE:
            raise CaseFailure(
                f"authoritative price must stay {_INITIAL_PRICE}, got {price}"
            )

        report = await _recover(env, executor_id="recovery-restart-executor")
        if run_id not in report.advanced:
            raise CaseFailure(f"the restart scan must advance the interrupted run: {report}")
        resumed = await _read_run(engine, env.workspace_id, run_id)
        if resumed.status.value != "WAITING_INPUT":
            raise CaseFailure(
                "the restart must resume the run up to the next input boundary, got "
                f"{resumed.status.value}"
            )
        stale = await _latest_attempt(engine, env.workspace_id, run_id)
        if stale is None or stale.attempt_no != 2:
            raise CaseFailure("the restart must open a second attempt")
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            abandoned = (
                await session.scalars(
                    select(RunAttempt)
                    .where(RunAttempt.id == attempt_id)
                    .execution_options(populate_existing=True)
                )
            ).one()
        finally:
            await session.close()
        if abandoned.status is not RunAttemptStatus.ABANDONED:
            raise CaseFailure(
                "the crashed attempt must be closed as ABANDONED, got "
                f"{abandoned.status.value}"
            )
        if abandoned.failure_code != "EXECUTOR_RESTART":
            raise CaseFailure(
                f"the crashed attempt must record EXECUTOR_RESTART, got {abandoned.failure_code!r}"
            )
        resumed_writes = await _logical_writes(env, run_id)
        if resumed_writes != 0:
            raise CaseFailure(
                f"resuming before the write must not write, got {resumed_writes}"
            )
        if await _authoritative_price(env) != _INITIAL_PRICE:
            raise CaseFailure("resuming before the write must not change the price")
        return _CaseObservation(
            run_status=resumed.status.value,
            price=_INITIAL_PRICE,
            logical_writes=resumed_writes,
            reconcile_path=(),
        )


async def _case_write_applied_response_lost(engine: AsyncEngine) -> _CaseObservation:
    """c) write-applied-response-lost:外部写生效响应丢失 → UNKNOWN → 回查 APPLIED。

    首版由执行器在本次推进内完成回查:``confirm`` 返回时 Run 已按 APPLIED 续推
    到终态,不再把 WAITING_RECONCILIATION 暴露给调用方。
    """
    async with _isolated_environment(engine, "write-applied-response-lost", _PriceWriteBackend()) as env:
        env.commerce_write_observer.faults.inject(RESPONSE_LOST_AFTER_COMMIT)
        pending = await _drive_to_confirmation(env)
        view = await env.application.confirm(pending.confirmation_id)
        if view.status is not RunStatus.COMPLETED:
            raise CaseFailure(
                "response loss must be reconciled inside the same advance, got "
                f"{view.status.value}"
            )
        operation = await _single_operation(engine, env.workspace_id, pending.run_id)
        if operation is None or operation.status.value != "APPLIED":
            raise CaseFailure(
                "the in-line reconcile must settle the operation as APPLIED, got "
                f"{None if operation is None else operation.status.value}"
            )
        return await _assert_completed_write(
            env, pending.run_id, expected_path=("UNKNOWN", "APPLIED")
        )


async def _case_executor_restart_after_confirmation(engine: AsyncEngine) -> _CaseObservation:
    """d) executor-restart-after-confirmation:确认接受后执行器崩溃 → 重启推进到 COMPLETED。"""
    async with _isolated_environment(engine, "executor-restart-after-confirmation", _PriceWriteBackend()) as env:
        pending = await _drive_to_confirmation(env)
        await _accept_confirmation(engine, env, pending)
        await _interrupt_run(
            engine, env, pending.run_id, executor_id="recovery-lost-executor"
        )
        run = await _read_run(engine, env.workspace_id, pending.run_id)
        if run.status.value != "RUNNING":
            raise CaseFailure(f"the crash must leave the run RUNNING, got {run.status.value}")
        writes_before = await _logical_writes(env, pending.run_id)
        if writes_before != 0:
            raise CaseFailure(
                f"no write may happen before the restart, got {writes_before}"
            )
        # 新进程启动:恢复扫描接管中断的 Run 并继续推进。
        report = await _recover(env, executor_id="recovery-restart-executor")
        if pending.run_id not in report.advanced:
            raise CaseFailure(f"the restart scan must advance the run: {report}")
        run = await _read_run(engine, env.workspace_id, pending.run_id)
        if run.status.value != "COMPLETED":
            raise CaseFailure(
                f"run expected COMPLETED after the restart, got {run.status.value}"
            )
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            request = (
                await session.scalars(
                    select(ConfirmationRequest).where(
                        ConfirmationRequest.workspace_id == env.workspace_id,
                        ConfirmationRequest.id == pending.confirmation_id,
                    )
                )
            ).one()
        finally:
            await session.close()
        if request.status.value != "ACCEPTED":
            raise CaseFailure(
                f"the confirmation must stay ACCEPTED, got {request.status.value}"
            )
        return await _assert_completed_write(env, pending.run_id, expected_path=())


async def _case_platform_persistent_failure(engine: AsyncEngine) -> _CaseObservation:
    """e) platform-persistent-failure:平台持续失败 → 确定 NOT_APPLIED,零写。

    持续故障不是"暂时不可用":回执确定 FAILED 且不声明可重试,因此 Operation
    NOT_APPLIED、ToolCall 落 FAILED 终态、失败返回模型,Run 不重排队、不产生
    UNKNOWN 回查。(用 ``permanent_failure`` 模拟"暂时不可用"会驱动 Runtime 进入
    一个没有成功出口的重排循环,故本用例只声明持续故障的确定语义。)
    """
    async with _isolated_environment(
        engine, "platform-persistent-failure", _PriceWriteBackend()
    ) as env:
        env.commerce_write_observer.faults.inject(PERMANENT_FAILURE)
        pending = await _drive_to_confirmation(env)
        view = await env.application.confirm(pending.confirmation_id)
        if view.status is not RunStatus.COMPLETED:
            raise CaseFailure(
                "a persistent platform failure must reach a terminal run state, got "
                f"{view.status.value}"
            )
        operation = await _single_operation(engine, env.workspace_id, pending.run_id)
        if operation is None or operation.status.value != "NOT_APPLIED":
            raise CaseFailure(
                "operation must be NOT_APPLIED (no UNKNOWN guessing), got "
                f"{None if operation is None else operation.status.value}"
            )
        receipt = operation.last_receipt
        if (
            not isinstance(receipt, dict)
            or receipt.get("outcome") != "NOT_APPLIED"
            or receipt.get("error_code") != "PLATFORM_FAILURE"
        ):
            raise CaseFailure(
                f"authoritative receipt must record NOT_APPLIED/PLATFORM_FAILURE, got {receipt!r}"
            )
        tool_call = await _single_tool_call(engine, env.workspace_id, pending.run_id)
        if tool_call.status.value != "FAILED":
            raise CaseFailure(
                "a persistent platform failure must terminate the tool call "
                f"(no requeue for revalidation), got {tool_call.status.value}"
            )
        if await _count_operations(engine, env.workspace_id, pending.run_id) != 1:
            raise CaseFailure("exactly one authoritative operation is allowed")
        writes = await _logical_writes(env, pending.run_id)
        if writes != 0:
            raise CaseFailure(f"platform failure must not write, got {writes} writes")
        price = await _authoritative_price(env)
        if price != _INITIAL_PRICE:
            raise CaseFailure(
                f"authoritative state must stay unchanged ({_INITIAL_PRICE}), got {price}"
            )
        path = await _reconcile_path(engine, env.workspace_id, pending.run_id)
        if path:
            raise CaseFailure(
                "a deterministic platform failure must never produce UNKNOWN, got "
                f"{list(path)}"
            )
        return _CaseObservation(
            run_status=view.status.value,
            price=price,
            logical_writes=writes,
            reconcile_path=(),
        )


async def _case_reconcile_temporarily_inconsistent(engine: AsyncEngine) -> _CaseObservation:
    """f) reconcile-temporarily-inconsistent:首次回查 UNKNOWN,再次 APPLIED → COMPLETED。

    回查不确定时 Run 停在 WAITING_RECONCILIATION(不猜测结果);``recover`` 在
    回查预算内重试,第二次拿到 APPLIED 后恢复推进到 COMPLETED。
    """
    async with _isolated_environment(engine, "reconcile-temporarily-inconsistent", _PriceWriteBackend()) as env:
        faults = env.commerce_write_observer.faults
        faults.inject(RESPONSE_LOST_AFTER_COMMIT)
        faults.inject(TEMPORARY_RECONCILE_UNKNOWN)
        pending = await _drive_to_confirmation(env)
        view = await env.application.confirm(pending.confirmation_id)
        if view.status is not RunStatus.WAITING_RECONCILIATION:
            raise CaseFailure(
                "an uncertain reconcile must park the run in WAITING_RECONCILIATION, got "
                f"{view.status.value}"
            )
        operation = await _single_operation(engine, env.workspace_id, pending.run_id)
        if operation is None or operation.status.value != "UNKNOWN":
            raise CaseFailure("operation must stay UNKNOWN after an uncertain reconcile")
        recovered = await env.application.recover(pending.run_id)
        if recovered.status is not RunStatus.COMPLETED:
            raise CaseFailure(
                f"run expected COMPLETED after the retried reconcile, got {recovered.status.value}"
            )
        return await _assert_completed_write(
            env, pending.run_id, expected_path=("UNKNOWN", "UNKNOWN", "APPLIED")
        )


async def _case_duplicate_submit(engine: AsyncEngine) -> _CaseObservation:
    """g) duplicate-submit:同 operation_id 重复提交 → 幂等回放,仍一次写。"""
    async with _isolated_environment(engine, "duplicate-submit", _PriceWriteBackend()) as env:
        pending = await _drive_to_confirmation(env)
        view = await env.application.confirm(pending.confirmation_id)
        if view.status is not RunStatus.COMPLETED:
            raise CaseFailure(
                f"run expected COMPLETED before the duplicate submit, got {view.status.value}"
            )
        tool_call = await _single_tool_call(engine, env.workspace_id, pending.run_id)
        if tool_call.operation_id is None:
            raise CaseFailure("the executed write must carry a stable operation_id")
        operation = await _single_operation(engine, env.workspace_id, pending.run_id)
        if operation is None:
            raise CaseFailure("the executed write must persist an operation")
        expected = operation.expected_state
        if expected is None:
            raise CaseFailure("the executed write must retain expected_state")
        # 重复提交:同 operation_id 经真实执行端点再次到达平台;平台按幂等键
        # 回放上次回执,不产生第二次逻辑写。
        receipt = await env.commerce_write_observer.set_price(
            SetListingPriceCommand(
                operation_id=str(tool_call.operation_id),
                idempotency_key=f"op:{tool_call.operation_id}",
                workspace_id=uuid.UUID(expected["workspace_id"]),
                store_id=uuid.UUID(expected["store_id"]),
                listing_id=uuid.UUID(expected["listing_id"]),
                external_store_ref=expected["external_store_ref"],
                external_product_ref=expected["external_product_ref"],
                sku=expected["sku"],
                target_price=Decimal(expected["target_price"]),
                currency=expected["currency"],
                expected_version=int(expected["expected_version"]),
            )
        )
        if receipt.status.value != "APPLIED":
            raise CaseFailure(
                f"duplicate submit must replay the applied receipt, got {receipt.status!r}"
            )
        if await env.commerce_write_observer.count_logical_writes(
            str(tool_call.operation_id)
        ) != 1:
            raise CaseFailure("the platform must have performed the write exactly once")
        if await _count_operations(engine, env.workspace_id, pending.run_id) != 1:
            raise CaseFailure("a duplicate submit must not register a second operation")
        writes = await _logical_writes(env, pending.run_id)
        if writes != 1:
            raise CaseFailure(
                f"idempotent replay must not produce a second logical write, got {writes}"
            )
        price = await _authoritative_price(env)
        if price != _TARGET_PRICE:
            raise CaseFailure(
                f"authoritative price must stay at the first write ({_TARGET_PRICE}), "
                f"got {price}"
            )
        path = await _reconcile_path(engine, env.workspace_id, pending.run_id)
        if path:
            raise CaseFailure(
                f"a duplicate submit must never produce UNKNOWN, got {list(path)}"
            )
        return _CaseObservation(
            run_status=view.status.value,
            price=price,
            logical_writes=writes,
            reconcile_path=(),
        )


# ---------------------------------------------------------------------------
# 分派注册表:故障注名 → 确定性执行函数
# ---------------------------------------------------------------------------


FaultExecutor = Callable[[AsyncEngine], Awaitable[_CaseObservation]]

_FAULT_EXECUTORS: Final[Mapping[str, FaultExecutor]] = {
    "model_timeout": _case_model_timeout,
    "executor_exit_before_write": _case_executor_exit_before_write,
    "response_lost_after_commit": _case_write_applied_response_lost,
    "executor_restart_after_confirmation": _case_executor_restart_after_confirmation,
    "permanent_failure": _case_platform_persistent_failure,
    "temporary_reconcile_unknown": _case_reconcile_temporarily_inconsistent,
    "duplicate_submit": _case_duplicate_submit,
}


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


# ---------------------------------------------------------------------------
# 套件入口
# ---------------------------------------------------------------------------


_INFRASTRUCTURE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    # 连接失败:OSError 系(ConnectionError/TimeoutError 均为其子类)。
    OSError,
    # 依赖不可用:数据库连接/接口层错误。
    InterfaceError,
    OperationalError,
)


class RecoverySuite:
    """故障恢复 Eval 套件:从数据集声明加载,按故障注名分派确定性执行。"""

    def __init__(self, engine: AsyncEngine, *, dataset: Path | str) -> None:
        self._engine = engine
        self._cases = load_recovery_dataset(dataset)
        case_ids = [case.id for case in self._cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError(f"recovery dataset declares duplicate case ids: {case_ids}")
        if not self._cases:
            raise ValueError(f"recovery dataset {dataset} declares no cases")
        self._outcomes: dict[str, RecoveryCaseOutcome] = {
            case.id: self._declared_outcome(case) for case in self._cases
        }

    @property
    def case_ids(self) -> frozenset[str]:
        return frozenset(case.id for case in self._cases)

    def result_for(self, case_id: str) -> RecoveryCaseOutcome:
        """按 id 取用例结果;run_all() 之前返回仅含声明期望的 SKIPPED 视图。"""
        try:
            return self._outcomes[case_id]
        except KeyError:
            raise KeyError(
                f"recovery dataset declares no case {case_id!r}"
            ) from None

    async def run_all(self, *, skip_ids: frozenset[str] | None = None) -> RecoverySuiteReport:
        """逐个执行用例(按 id 稳定排序);断言失败与基础设施失败分开报告。"""
        skips = skip_ids if skip_ids is not None else frozenset[str]()
        results: list[RecoveryCaseOutcome] = []
        for case in sorted(self._cases, key=lambda item: item.id):
            outcome = (
                self._outcome(case, status="SKIPPED", observation=None, error=None)
                if case.id in skips
                else await self._run_case(case)
            )
            self._outcomes[case.id] = outcome
            results.append(outcome)
        return RecoverySuiteReport(results=tuple(results))

    # -- 内部 ------------------------------------------------------------

    async def _run_case(self, case: RecoveryCase) -> RecoveryCaseOutcome:
        executor = _FAULT_EXECUTORS[case.fault]
        try:
            observation = await executor(self._engine)
        except _INFRASTRUCTURE_EXCEPTIONS as error:
            return self._outcome(case, status="INFRASTRUCTURE_ERROR", observation=None, error=error)
        except Exception as error:  # noqa: BLE001 - 不变式破坏按确定性失败报告
            return self._outcome(case, status="FAILED", observation=None, error=error)
        return self._judge(case, observation)

    def _judge(
        self, case: RecoveryCase, observation: _CaseObservation
    ) -> RecoveryCaseOutcome:
        """观测事实与数据集声明对账:回查路径、逻辑写上界与权威最终状态。"""
        failures: list[str] = []
        path_matches = list(observation.reconcile_path) == case.expected_reconcile_path
        if not path_matches:
            failures.append(
                f"reconcile path {list(observation.reconcile_path)} != expected "
                f"{case.expected_reconcile_path}"
            )
        if observation.logical_writes > case.max_logical_writes:
            failures.append(
                f"logical writes {observation.logical_writes} exceed the declared "
                f"maximum {case.max_logical_writes}"
            )
        if observation.price != case.expected_final_state.price:
            failures.append(
                f"authoritative price {observation.price} != expected "
                f"{case.expected_final_state.price}"
            )
        if observation.run_status != case.expected_final_state.run_status:
            failures.append(
                f"run status {observation.run_status} != expected "
                f"{case.expected_final_state.run_status}"
            )
        if failures:
            return self._outcome(
                case,
                status="FAILED",
                observation=observation,
                error=CaseFailure("; ".join(failures)),
            )
        return self._outcome(case, status="PASSED", observation=observation, error=None)

    def _declared_outcome(self, case: RecoveryCase) -> RecoveryCaseOutcome:
        """run_all() 之前的结果视图:仅含声明期望(SKIPPED 表示尚未执行)。"""
        return RecoveryCaseOutcome(
            case_id=case.id,
            category=case.category,
            risk_level=case.risk_level,
            fault=case.fault,
            expectation=case.expectation,
            status="SKIPPED",
            passed=False,
            included_in_quality_score=False,
            error=None,
            expected_reconcile_path=list(case.expected_reconcile_path),
            max_logical_writes=case.max_logical_writes,
            expected_final_state=case.expected_final_state,
        )

    @staticmethod
    def _outcome(
        case: RecoveryCase,
        *,
        status: CaseStatus,
        observation: _CaseObservation | None,
        error: BaseException | None,
    ) -> RecoveryCaseOutcome:
        if observation is None:
            return RecoveryCaseOutcome(
                case_id=case.id,
                category=case.category,
                risk_level=case.risk_level,
                fault=case.fault,
                expectation=case.expectation,
                status=status,
                passed=status == "PASSED",
                included_in_quality_score=status == "PASSED",
                error=None if error is None else _describe(error),
                expected_reconcile_path=list(case.expected_reconcile_path),
                max_logical_writes=case.max_logical_writes,
                expected_final_state=case.expected_final_state,
            )
        path_matches = list(observation.reconcile_path) == case.expected_reconcile_path
        final_state_matches = (
            observation.price == case.expected_final_state.price
            and observation.run_status == case.expected_final_state.run_status
        )
        return RecoveryCaseOutcome(
            case_id=case.id,
            category=case.category,
            risk_level=case.risk_level,
            fault=case.fault,
            expectation=case.expectation,
            status=status,
            passed=status == "PASSED",
            included_in_quality_score=status == "PASSED",
            error=None if error is None else _describe(error),
            expected_reconcile_path=list(case.expected_reconcile_path),
            max_logical_writes=case.max_logical_writes,
            expected_final_state=case.expected_final_state,
            observed_reconcile_path=list(observation.reconcile_path),
            observed_logical_writes=observation.logical_writes,
            observed_price=observation.price,
            observed_run_status=observation.run_status,
            path_matches=path_matches,
            final_state_matches=final_state_matches,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_DATABASE_URL_ENV: Final = "RUNTIME_TEST_DATABASE_URL"
_DEFAULT_DATABASE_URL: Final = (
    "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime_test"
)


def _database_url() -> str:
    return os.environ.get(_DATABASE_URL_ENV, _DEFAULT_DATABASE_URL)


def _database_name(database_url: str) -> str:
    return database_url.rsplit("/", 1)[-1].split("?", 1)[0]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = database_url
    return config


async def _create_database_if_missing(database_url: str, name: str) -> None:
    admin_url = database_url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            exists = await connection.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            )
            if exists.scalar() is None:
                await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin_engine.dispose()


def _prepare_database(database_url: str) -> None:
    """确保测试库存在并应用迁移(幂等、非破坏性:不清空不回滚任何数据)。"""
    name = _database_name(database_url)
    if "test" not in name:
        raise RuntimeError(
            "recovery suite refuses to run against a non-test database "
            f"({name!r}); set {_DATABASE_URL_ENV} to an *_test database"
        )
    asyncio.run(_create_database_if_missing(database_url, name))
    command.upgrade(_alembic_config(database_url), "head")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.recovery_suite",
        description="Run the deterministic fault recovery eval suite.",
    )
    parser.add_argument(
        "--dataset",
        default=str(RECOVERY_DATASET_PATH),
        help="dataset file or directory declaring recovery cases (YAML)",
    )
    args = parser.parse_args(argv)
    database_url = _database_url()
    _prepare_database(database_url)
    engine = create_async_engine(database_url)
    try:
        suite = RecoverySuite(engine, dataset=args.dataset)
        report = asyncio.run(suite.run_all())
    finally:
        asyncio.run(engine.dispose())
    print(report.summary_line())
    print(
        f"recovery_success_rate={report.recovery_success_rate:.2f} "
        f"final_state_consistency={report.final_state_consistency:.2f} "
        f"unknown_handling_correct={report.unknown_handling_correct:.2f}"
    )
    for result in report.results:
        marker = "PASS" if result.passed else result.status
        line = (
            f"  {marker} {result.case_id} [{result.category}/{result.risk_level}] "
            f"writes={result.observed_logical_writes}/{result.max_logical_writes}"
        )
        if result.error is not None:
            line = f"{line} :: {result.error}"
        print(line)
    if (
        report.failed
        or report.infrastructure_errors
        or report.skipped
        or report.actual_duplicate_writes
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
