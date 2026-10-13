"""模型可见店铺引用 → 内部 Store UUID 的授权解析(P1 Task 5;设计 §4.1)。

设计 §4.1 规定模型只能填写 ``store_ref``(用户说出的名称或别名),
``store_id`` 等内部标识由服务端解析。本模块提供该解析的唯一入口
:class:`StoreReferenceResolver`,供 Catalog / Listing 等能力模块的 Service
在 PreToolUse 之后、构造可信查询之前调用。

语义与既有 :mod:`agents.product_operations.tools.access` 一致,不做静默收窄:

- 引用无匹配 → ``NOT_FOUND``;
- 同一引用匹配到多个店铺(名称/别名冲突)→ ``AMBIGUOUS_STORE_REFERENCE``;
- 引用命中店铺但不在 Actor 授权范围 → ``RESOURCE_SCOPE_DENIED``(显式拒绝,
  不静默丢弃);
- 不接受 UUID 形式的内部标识:模型不得填写内部 ID。

错误通道固定为两类:授权与资源范围拒绝沿用既有读路径的
:class:`ToolAccessError`(PreToolUse 管线已识别该类型),领域查找失败用
:class:`ProductOperationError`(规范错误码)。

``tools.access`` / ``tools.errors`` 在**调用点**导入:``tools`` 包的
``__init__`` 会拉起 ``tools.compare_listing_prices``,后者又依赖
``input.profiles``,模块级互相 import 会形成 ``input`` ↔ ``tools`` 循环。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StoreStatus
from agents.product_operations.persistence.models import Store
from agents.product_operations.shared.errors import (
    AMBIGUOUS_STORE_REFERENCE,
    NOT_FOUND,
    ProductOperationError,
)
from runtime.access.actor import ActorContext
from runtime.access.authorization import RESOURCE_SCOPE_DENIED


@dataclass(frozen=True)
class _StoreCandidate:
    """工作区内一个 ACTIVE 店铺的可见引用面。"""

    store_id: uuid.UUID
    name: str
    aliases: tuple[str, ...]

    def matches(self, reference: str) -> bool:
        return reference == self.name or reference in self.aliases


class StoreReferenceResolver:
    """把 ``store_ref`` / ``store_refs`` 解析为授权范围内的 Store UUID。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_authorized(self, actor: ActorContext, store_ref: str) -> uuid.UUID:
        """解析单个店铺引用;候选为当前 Workspace 全部 ACTIVE 店铺,再校验授权。"""
        candidates = await self._load_active_stores(actor)
        return self._resolve_one(actor, store_ref, candidates)

    async def resolve_authorized_many(
        self, actor: ActorContext, store_refs: Sequence[str]
    ) -> tuple[uuid.UUID, ...]:
        """解析多个店铺引用;空集合表示"全部授权店铺"。

        返回按输入顺序去重的内部 Store UUID 元组;``store_refs`` 为空时返回
        Actor 授权范围内全部 ACTIVE 店铺(按店铺名与 ID 确定排序)。
        """
        candidates = await self._load_active_stores(actor)
        if not store_refs:
            # 调用点导入打破 ``input`` ↔ ``tools`` 的模块级环(见文件头说明)。
            from agents.product_operations.tools.access import authorized_store_ids

            authorized = authorized_store_ids(actor)
            return tuple(
                candidate.store_id
                for candidate in candidates
                if authorized is None or str(candidate.store_id) in authorized
            )
        resolved: list[uuid.UUID] = []
        for reference in store_refs:
            store_id = self._resolve_one(actor, reference, candidates)
            if store_id not in resolved:
                resolved.append(store_id)
        return tuple(resolved)

    def _resolve_one(
        self,
        actor: ActorContext,
        reference: str,
        candidates: Sequence[_StoreCandidate],
    ) -> uuid.UUID:
        normalized = reference.strip()
        if not normalized:
            raise ProductOperationError(NOT_FOUND, "store_ref 为空,无法解析店铺")
        matches = [candidate for candidate in candidates if candidate.matches(normalized)]
        if not matches:
            raise ProductOperationError(
                NOT_FOUND, f"店铺引用 {reference!r} 在当前 Workspace 无匹配"
            )
        if len(matches) > 1:
            raise ProductOperationError(
                AMBIGUOUS_STORE_REFERENCE,
                f"店铺引用 {reference!r} 匹配到 {len(matches)} 个店铺,无法确定性解析",
            )
        store_id = matches[0].store_id
        # 调用点导入打破 ``input`` ↔ ``tools`` 的模块级环(见文件头说明)。
        from agents.product_operations.tools.access import authorized_store_ids
        from agents.product_operations.tools.errors import ToolAccessError

        authorized = authorized_store_ids(actor)
        if authorized is not None and str(store_id) not in authorized:
            raise ToolAccessError(
                RESOURCE_SCOPE_DENIED,
                f"店铺引用 {reference!r} 不在当前授权范围内",
            )
        return store_id

    async def _load_active_stores(self, actor: ActorContext) -> tuple[_StoreCandidate, ...]:
        """当前 Workspace 全部 ACTIVE 店铺;授权范围只用于拒绝,不用于收窄。"""
        stmt = (
            select(Store)
            .where(
                Store.workspace_id == actor.workspace_id,
                Store.status == StoreStatus.ACTIVE,
            )
            .order_by(Store.name, Store.id)
        )
        rows = (await self._session.scalars(stmt)).all()
        return tuple(
            _StoreCandidate(
                store_id=store.id, name=store.name, aliases=tuple(store.aliases)
            )
            for store in rows
        )


__all__ = ["AMBIGUOUS_STORE_REFERENCE", "StoreReferenceResolver"]
