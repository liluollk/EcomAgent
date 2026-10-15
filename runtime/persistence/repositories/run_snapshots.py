"""Run 版本快照关联表仓库(计划 02 Task 2;数据模型文档 §5)。

覆盖 ``run_tool_versions`` / ``run_skill_versions`` / ``run_source_versions`` /
``run_policy_snapshots``,固定当次运行实际使用的精确版本;与其它 Repository
一样只执行 flush 与条件语句,绝不调用 commit —— 事务边界由调用方拥有。

``PolicySnapshotPin`` 是 Runtime 的**通用**值对象:它只知道"某个策略 key 的哪一
个版本行、内容 hash 是多少",不知道商品、价格或 ``product-pricing`` 的存在。
业务语义留在组合根,由它把自己的策略快照翻译成 pin。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.models.execution import (
    RunPolicySnapshot,
    RunSkillVersion,
    RunSourceVersion,
    RunToolVersion,
)


@dataclass(frozen=True)
class PolicySnapshotPin:
    """Run 创建时钉住的一条业务策略版本。

    只携带"读的是哪一行"与"内容 hash":足以事后解释模型当时看到的规则,又不会
    把任何业务字段带进 Runtime。
    """

    policy_key: str
    version_id: UUID
    config_hash: str


class RunSnapshotRepository:
    """Run 版本快照的幂等写入与读取。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace(
        self,
        *,
        run_id: UUID,
        workspace_id: UUID,
        tool_version_ids: Iterable[UUID] = (),
        skill_version_ids: Iterable[UUID] = (),
        source_version_ids: Iterable[UUID] = (),
        policy_pins: Iterable[PolicySnapshotPin] = (),
    ) -> None:
        """以给定集合整体替换 Run 的版本快照(创建与恢复重放均幂等)。

        四类快照在同一事务内整体替换:先把该 Run 的旧行全部删除,再按本次集合
        插入,因此"重复 replace"不增行、也不会残留上一轮不相干的快照。

        ``workspace_id`` 为 Run 所属 Workspace:Skill/Tool 快照可引用
        GLOBAL 版本(单列外键),Source/Policy 快照使用 (workspace_id, id)
        复合外键。
        """
        for model in (
            RunToolVersion,
            RunSkillVersion,
            RunSourceVersion,
            RunPolicySnapshot,
        ):
            await self._session.execute(
                sa.delete(model).where(
                    model.run_id == run_id,
                    model.workspace_id == workspace_id,
                )
            )
        self._session.add_all(
            RunToolVersion(run_id=run_id, workspace_id=workspace_id, tool_version_id=version_id)
            for version_id in tool_version_ids
        )
        self._session.add_all(
            RunSkillVersion(run_id=run_id, workspace_id=workspace_id, skill_version_id=version_id)
            for version_id in skill_version_ids
        )
        self._session.add_all(
            RunSourceVersion(
                run_id=run_id, workspace_id=workspace_id, source_version_id=version_id
            )
            for version_id in source_version_ids
        )
        self._session.add_all(
            RunPolicySnapshot(
                run_id=run_id,
                policy_key=pin.policy_key,
                workspace_id=workspace_id,
                proposed_version_id=pin.version_id,
                config_hash=pin.config_hash,
            )
            for pin in policy_pins
        )
        await self._session.flush()

    async def tool_version_ids(self, run_id: UUID) -> list[UUID]:
        stmt = select(RunToolVersion.tool_version_id).where(RunToolVersion.run_id == run_id)
        return list(await self._session.scalars(stmt))

    async def skill_version_ids(self, run_id: UUID) -> list[UUID]:
        stmt = select(RunSkillVersion.skill_version_id).where(RunSkillVersion.run_id == run_id)
        return list(await self._session.scalars(stmt))

    async def source_version_ids(self, run_id: UUID) -> list[UUID]:
        stmt = select(RunSourceVersion.source_version_id).where(RunSourceVersion.run_id == run_id)
        return list(await self._session.scalars(stmt))

    async def policy_pins(self, run_id: UUID) -> list[PolicySnapshotPin]:
        """按 ``policy_key`` 读取该 Run 钉住的策略版本(hash 一并带回)。"""
        stmt = (
            select(
                RunPolicySnapshot.policy_key,
                RunPolicySnapshot.proposed_version_id,
                RunPolicySnapshot.config_hash,
            )
            .where(RunPolicySnapshot.run_id == run_id)
            .order_by(RunPolicySnapshot.policy_key)
        )
        return [
            PolicySnapshotPin(
                policy_key=policy_key,
                version_id=version_id,
                config_hash=config_hash,
            )
            for policy_key, version_id, config_hash in await self._session.execute(stmt)
            if version_id is not None
        ]


__all__ = ["PolicySnapshotPin", "RunSnapshotRepository"]
