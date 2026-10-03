"""最小写前授权服务:Run 版本已钉住,授权始终读当前库态(计划 02 Task 2)。

完整 PreToolUse 管线由后续任务实现;本服务只做"写前读当前授权"一件事:

- 从 ``run_tool_versions`` 取出该 Run 钉住的 Tool 版本,并取其本地分类
  声明的 ``required_permissions``;
- 经 ``ActorContextBuilder`` 从当前 PostgreSQL 状态重建 ActorContext
  (角色、动作权限、资源范围全部来自当前库态,绝不来自钉住的快照);
- ``Authorizer`` 判定:钉住版本要求、而当前缺失的动作权限 =>
  ``PERMISSION_REVOKED`` —— Run 创建时的装配校验保证这些权限当时存在,
  之后缺失即说明已被 revoke;其余拒绝沿用判定结果的原因码;
- Membership/用户/Workspace 缺失或禁用 => ``ACTOR_UNAVAILABLE``。

不执行任何真实 Tool;Tool 执行与 PostToolUse 管线在后续任务接入。
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.access.actor import ActorContextBuilder, ActorContextError
from runtime.access.authorization import Authorizer
from runtime.capabilities.assembly import RuntimeAssembly
from runtime.capabilities.registry import ToolVersionRecord
from runtime.persistence.models.execution import Run
from runtime.persistence.repositories.run_snapshots import RunSnapshotRepository


PERMISSION_REVOKED: Final = "PERMISSION_REVOKED"
ACTOR_UNAVAILABLE: Final = "ACTOR_UNAVAILABLE"


class WriteExecutionError(Exception):
    """写前授权无法进行:Run 不存在,或请求的 Tool 未被该 Run 钉住。"""


class ToolCallRequest(BaseModel):
    """模型发起的 Tool 调用请求(Task 6 前的最小形态)。"""

    model_config = ConfigDict(frozen=True)

    tool_key: str
    arguments: dict[str, Any] = {}


class WriteExecutionResult(BaseModel):
    """写前授权结果;``allowed=False`` 时 ``denial_code`` 说明原因。"""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    denial_code: str | None = None


class WriteAuthorizationService:
    """写前读当前授权;版本用钉住集合,权限、范围读当前值。"""

    def __init__(self, *, session: AsyncSession, assembly: RuntimeAssembly) -> None:
        self._session = session
        self._assembly = assembly

    async def execute(self, run_id: UUID, call: ToolCallRequest) -> WriteExecutionResult:
        """对 ``run_id`` 上对 ``call.tool_key`` 的写请求做当前授权判定。"""
        run = await self._session.get(Run, run_id)
        if run is None:
            raise WriteExecutionError(f"run {run_id} 不存在")
        record = await self._pinned_tool_record(run, call.tool_key)

        try:
            actor = await ActorContextBuilder(self._session).build(
                actor_id=run.created_by_actor_id,
                workspace_id=run.workspace_id,
                request_id=f"write-{run_id}",
            )
        except ActorContextError:
            return WriteExecutionResult(allowed=False, denial_code=ACTOR_UNAVAILABLE)

        decision = Authorizer().authorize(
            actor,
            required_permissions=record.required_permissions,
            resources=frozenset(),
        )
        if decision.allowed:
            return WriteExecutionResult(allowed=True)
        revoked = bool(record.required_permissions - actor.permissions)
        if revoked:
            return WriteExecutionResult(allowed=False, denial_code=PERMISSION_REVOKED)
        return WriteExecutionResult(allowed=False, denial_code=decision.reason_code)

    async def _pinned_tool_record(self, run: Run, tool_key: str) -> ToolVersionRecord:
        """在该 Run 钉住的 Tool 版本中定位请求的稳定 Key。"""
        pinned_ids = await RunSnapshotRepository(self._session).tool_version_ids(run.id)
        for version_id in pinned_ids:
            record = self._assembly.tools.find_by_version_id(version_id)
            if record is not None and record.stable_key == tool_key:
                return record
        raise WriteExecutionError(f"run {run.id} 未钉住 Tool {tool_key}")


__all__ = [
    "ACTOR_UNAVAILABLE",
    "PERMISSION_REVOKED",
    "ToolCallRequest",
    "WriteAuthorizationService",
    "WriteExecutionError",
    "WriteExecutionResult",
]
