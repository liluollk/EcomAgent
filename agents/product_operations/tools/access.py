"""只读 Tool 的授权过滤(计划 03 Task 2;架构文档 §4、§6)。

查询始终携带 ``actor.workspace_id``;店铺范围与阶段 1 ResourceScope 语义
一致(``resource_type = "store"``,resource_id = ``str(store.id)``):

- 无 ``store`` scope 映射 => 不可见任何店铺(空结果即拒绝语义);
- ``mode = ALL`` => 当前 Workspace 全部店铺;
- ``mode = SELECTED`` => 仅显式 Store ID 集合。

动作权限复用 Runtime ``Authorizer``:缺少 ``product.read`` 以
``PERMISSION_DENIED`` 显式拒绝;显式传入的 store 不在授权范围内时以
``RESOURCE_SCOPE_DENIED`` 拒绝,不做静默收窄。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext
from runtime.access.authorization import (
    PERMISSION_DENIED,
    RESOURCE_SCOPE_DENIED,
    Authorizer,
    Decision,
)


PRODUCT_READ_PERMISSION: Final = "product.read"
STORE_RESOURCE_TYPE: Final = "store"

_AUTHORIZER: Final = Authorizer()


def authorize_product_read(actor: ActorContext) -> None:
    """校验动作权限;缺少 ``product.read`` 立即拒绝。"""
    _require_allowed(
        _AUTHORIZER.authorize(
            actor,
            required_permissions=frozenset({PRODUCT_READ_PERMISSION}),
            resources=frozenset(),
        )
    )


def authorize_store(actor: ActorContext, *, store_id: str) -> None:
    """显式 store 读取的权限 + 资源范围校验。"""
    _require_allowed(
        _AUTHORIZER.authorize(
            actor,
            required_permissions=frozenset({PRODUCT_READ_PERMISSION}),
            resources=frozenset({(STORE_RESOURCE_TYPE, store_id)}),
        )
    )


def authorized_store_ids(actor: ActorContext) -> frozenset[str] | None:
    """解析授权 Store ID 集合;``None`` 表示 ALL(Workspace 全部店铺)。

    无 ``store`` scope 映射返回空集合(= 无店铺可见)。
    """
    scope = actor.resource_scopes.get(STORE_RESOURCE_TYPE)
    if scope is None:
        return frozenset()
    if scope.mode == "ALL":
        return None
    return frozenset(scope.resource_ids)


def ensure_stores_in_scope(actor: ActorContext, store_ids: Sequence[str]) -> None:
    """显式传入的 store 必须落在授权范围内,否则拒绝。"""
    authorized = authorized_store_ids(actor)
    if authorized is None:
        return
    for store_id in store_ids:
        if store_id not in authorized:
            raise ToolAccessError(
                RESOURCE_SCOPE_DENIED,
                f"store {store_id} 不在当前授权范围内",
            )


def _require_allowed(decision: Decision) -> None:
    if decision.allowed:
        return
    raise ToolAccessError(
        decision.reason_code or PERMISSION_DENIED,
        "只读商品 Tool 拒绝执行",
    )


__all__ = [
    "PRODUCT_READ_PERMISSION",
    "STORE_RESOURCE_TYPE",
    "authorize_product_read",
    "authorize_store",
    "authorized_store_ids",
    "ensure_stores_in_scope",
]
