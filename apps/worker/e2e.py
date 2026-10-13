"""E2E 装配开关(阶段 4 Gate;``RUNTIME_E2E=1`` 时启用)。

让端到端验证在无真实模型、无真实平台的情况下走通完整语义:

- :class:`E2EScriptedBackend`:确定性脚本模型。脚本规则(写死,不猜测):

  1. 会话中尚无 ``tool`` 消息(首次模型轮):把最后一条用户消息按
     ``把 <SKU> 改成 <金额> 元`` 的固定正则解析出 ``sku`` 与
     ``target_price``(金额规范为两位小数字符串),发出一次
     ``update_price`` ToolCall;解析失败则回一段固定提示文本后正常完成;
  2. 已有 ``tool`` 消息(写操作回填结果之后):回一段固定完成文本,
     不再发出 ToolCall,Run 随之 COMPLETED。

  店铺不在用户消息里,由 PreToolUse 管线产生缺店铺 Input Request,
  经既有 API 端点回答;确认同样经既有端点接受。
- :class:`MockCommerceAdapter`(经既有 ``build_product_application`` 组装)
  真实写 ``product_operations.listings`` 权威价。
- :class:`E2EJobHandler`:Worker 进程的 Job 回调。按租约加载 Run 的
  workspace/actor,懒组装并缓存每 ``(workspace, actor)`` 的
  :class:`ProductApplication`,从持久化状态重建"已回答字段"视图后经
  ``ProductApplication.advance_leased`` 推进(不重复领取 Job)。

已回答字段的重建:InputService 回答时把值合并进 ToolCall 的
``normalized_arguments``,InputRequest 行本身不存值(审计也不落值)。
因此对每个 ANSWERED InputRequest,从其 ToolCall 挂起参数按
``field_paths`` 白名单取回回答值——这是对既有持久化语义的最小重建,
不改变 runtime/ 的任何行为。

依赖方向:本模块属于 apps 层,可 import runtime/agents/adapters;
runtime 不 import 本模块。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.policy.models import PricePolicyConfig
from apps.worker.assembly import (
    ProductApplication,
    build_product_application,
)
from composition.provisioning.product_operations import (
    ModelConnectionProvisioningSpec,
    ProductOperationsProvisioner,
)
from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)
from runtime.jobs.lease_service import Lease, LeaseOutcome
from runtime.persistence.base import (
    AccountStatus,
    InputRequestStatus,
    ModelBackendType,
    RunJobKind,
)
from runtime.persistence.models.execution import Run, RunJob, ToolCall
from runtime.persistence.models.identity import WorkspaceMembership
from runtime.persistence.models.interaction import InputRequest


E2E_PRICE_CONFIG: Final = PricePolicyConfig(
    policy_version="price-policy-v1",
    currency="CNY",
    min_allowed_price=Decimal("50.00"),
    max_price_change=Decimal("100.00"),
)
"""E2E 装配的价格策略:与商品域闭环测试一致(99 元对 128 元为合法变更)。"""

UPDATE_PRICE_TOOL_KEY: Final = "update_price"

E2E_MODEL_SPEC: Final = ModelConnectionProvisioningSpec(
    stable_key="product-scripted",
    backend_type=ModelBackendType.OPENAI,
    base_url="https://scripted.model.invalid/v1",
    model="product-scripted",
    non_secret_options={},
)
"""E2E 发布的确定性脚本模型规范(仅本地/CI 端到端验证使用)。"""

_PRICE_TASK_PATTERN: Final = re.compile(
    r"把\s*(?P<sku>[A-Za-z0-9][A-Za-z0-9_\-]*)\s*改成\s*"
    r"(?P<price>[0-9]+(?:\.[0-9]{1,2})?)\s*元"
)
"""E2E 脚本解析规则(写死):``把 <SKU> 改成 <金额> 元``;金额至多两位小数。"""

_UNPARSED_REPLY: Final = "无法解析改价指令,请使用“把 SKU-001 改成 99 元”格式。"
_DONE_REPLY: Final = "改价已执行完成。"


@dataclass(frozen=True)
class ParsedPriceTask:
    """一次成功解析的改价指令参数。"""

    sku: str
    target_price: str


def parse_price_task(text: str) -> ParsedPriceTask | None:
    """按固定正则解析用户消息;不匹配返回 None(不做任何模糊匹配)。"""
    match = _PRICE_TASK_PATTERN.search(text)
    if match is None:
        return None
    raw_price = match.group("price")
    # 规范化为两位小数字符串(与渠道金额格式一致)。
    return ParsedPriceTask(
        sku=match.group("sku"),
        target_price=str(Decimal(raw_price).quantize(Decimal("0.01"))),
    )


class E2EScriptedBackend:
    """确定性脚本模型:首次轮发 update_price ToolCall,写结果回填后收尾。"""

    def __init__(self, *, tool_key: str = UPDATE_PRICE_TOOL_KEY) -> None:
        self._tool_key = tool_key
        self.calls = 0
        """发出的模型轮次数(观测用)。"""

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        del cancellation  # 脚本无长阻塞点,不需要中途取消。
        self.calls += 1
        has_tool_result = any(message.role == "tool" for message in request.messages)
        if has_tool_result:
            # 写操作已回填结果:收尾文本,Run COMPLETED。
            yield TextDelta(text_delta=_DONE_REPLY)
            yield BackendCompleted(finish_reason="stop")
            return
        parsed = self._parse_current_task(request)
        if parsed is None:
            yield TextDelta(text_delta=_UNPARSED_REPLY)
            yield BackendCompleted(finish_reason="stop")
            return
        yield ToolCallCompleted(
            call_id=f"e2e-{uuid.uuid4()}",
            name=self._tool_key,
            arguments={
                "sku": parsed.sku,
                "target_price": parsed.target_price,
            },
        )
        yield BackendCompleted(finish_reason="stop")

    @staticmethod
    def _parse_current_task(request: BackendRequest) -> ParsedPriceTask | None:
        """最后一条用户消息即当前任务(会话内尚无 tool 结果)。"""
        for message in reversed(request.messages):
            if message.role == "user":
                return parse_price_task(message.content)
        return None


async def build_e2e_application(
    engine: AsyncEngine,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    membership_id: uuid.UUID,
    worker_id: str = "e2e-worker",
) -> ProductApplication:
    """E2E 组合根:ScriptedBackend + Mock Commerce Adapter 的完整商品应用环境。

    先经 :class:`ProductOperationsProvisioner` 显式发布 scripted 能力规范,再
    复用既有 :func:`build_product_application`(启动校验、已发布快照消费、
    PreToolUse 管线、MockCommerceAdapter 桥接),只把 Backend 换成
    :class:`E2EScriptedBackend`;不改 runtime/ 语义。
    """
    capabilities = await ProductOperationsProvisioner(
        lambda: AsyncSession(engine, expire_on_commit=False)
    ).provision(
        workspace_id=workspace_id,
        model=E2E_MODEL_SPEC,
        pricing_policy=E2E_PRICE_CONFIG,
    )
    return await build_product_application(
        engine,
        capabilities=capabilities,
        backend=E2EScriptedBackend(),
        workspace_id=workspace_id,
        actor_id=actor_id,
        membership_id=membership_id,
        worker_id=worker_id,
    )


async def rebuild_answered_view(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, str]:
    """从持久化状态重建 Run 的"已回答字段"视图(见模块 docstring)。"""
    requests = (
        await session.scalars(
            select(InputRequest).where(
                InputRequest.workspace_id == workspace_id,
                InputRequest.run_id == run_id,
                InputRequest.status == InputRequestStatus.ANSWERED,
            )
        )
    ).all()
    answered: dict[str, str] = {}
    for request in requests:
        if request.tool_call_id is None:
            continue
        tool_call = (
            await session.scalars(
                select(ToolCall).where(
                    ToolCall.workspace_id == workspace_id,
                    ToolCall.id == request.tool_call_id,
                )
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
    return answered


class E2EJobHandler:
    """``RUNTIME_E2E=1`` 的 Worker Job 回调:按租约推进已存在的 Run。

    每个 ``(workspace, actor)`` 懒组装并缓存一个 ProductApplication(能力版本
    落库幂等);推进经 ``ProductApplication.advance_leased`` 复用 Runtime 的
    单事务 AgentLoop 语义,WorkerHost 负责外层 claim/heartbeat/complete。
    """

    def __init__(self, engine: AsyncEngine, *, worker_id: str = "e2e-worker") -> None:
        self._engine = engine
        self._worker_id = worker_id
        self._applications: dict[
            tuple[uuid.UUID, uuid.UUID], ProductApplication
        ] = {}

    @property
    def applications(
        self,
    ) -> Mapping[tuple[uuid.UUID, uuid.UUID], ProductApplication]:
        return self._applications

    async def aclose(self) -> None:
        """关闭全部缓存商品应用的 Adapter Session(进程退出路径)。"""
        for application in self._applications.values():
            await application.aclose()
        self._applications.clear()

    async def __call__(self, lease: Lease) -> LeaseOutcome:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            context = await self._load_context(session, lease)
        finally:
            await session.close()
        if isinstance(context, LeaseOutcome):
            return context
        application = await self._application_for(context)
        return await application.advance_leased(lease, answered=context.answered)

    async def _application_for(
        self, context: _LeaseContext
    ) -> ProductApplication:
        key = (context.workspace_id, context.actor_id)
        application = self._applications.get(key)
        if application is None:
            application = await build_e2e_application(
                self._engine,
                workspace_id=context.workspace_id,
                actor_id=context.actor_id,
                membership_id=context.membership_id,
                worker_id=self._worker_id,
            )
            self._applications[key] = application
        return application

    async def _load_context(
        self, session: AsyncSession, lease: Lease
    ) -> _LeaseContext | LeaseOutcome:
        """加载租约上下文;不可推进时直接返回确定性结果(不抛暂时性错误)。"""
        job = (
            await session.scalars(select(RunJob).where(RunJob.id == lease.job_id))
        ).one_or_none()
        if job is None:
            return LeaseOutcome(failure_code="JOB_NOT_FOUND")
        if job.kind is RunJobKind.RECONCILE_OPERATION:
            # E2E 流程无故障注入,不产生 UNKNOWN 回执;显式拒绝而非静默跳过。
            return LeaseOutcome(failure_code="E2E_RECONCILE_UNSUPPORTED")
        run = (
            await session.scalars(
                select(Run).where(
                    Run.workspace_id == lease.workspace_id, Run.id == lease.run_id
                )
            )
        ).one_or_none()
        if run is None:
            return LeaseOutcome(failure_code="RUN_NOT_FOUND")
        membership_id = (
            await session.scalars(
                select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == lease.workspace_id,
                    WorkspaceMembership.user_id == run.created_by_actor_id,
                    WorkspaceMembership.status == AccountStatus.ACTIVE,
                )
            )
        ).one_or_none()
        if membership_id is None:
            return LeaseOutcome(failure_code="MEMBERSHIP_NOT_FOUND")
        answered = await rebuild_answered_view(
            session, lease.workspace_id, lease.run_id
        )
        return _LeaseContext(
            workspace_id=lease.workspace_id,
            actor_id=run.created_by_actor_id,
            membership_id=membership_id,
            answered=answered,
        )


@dataclass(frozen=True)
class _LeaseContext:
    """一次租约推进所需的上下文(租约本身不含 actor/回答视图)。"""

    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    membership_id: uuid.UUID
    answered: dict[str, str]


__all__ = [
    "E2E_PRICE_CONFIG",
    "E2EJobHandler",
    "E2EScriptedBackend",
    "ParsedPriceTask",
    "build_e2e_application",
    "parse_price_task",
    "rebuild_answered_view",
]
