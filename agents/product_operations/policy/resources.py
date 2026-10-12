"""商品域 AuthorizationResourceResolver(计划 03 Task 4;架构文档 §3)。

``update_price`` 的授权资源只有店铺:``sku``/``target_price`` 等不是可
授权资源,不进入资源引用集合。签名与 Runtime 注入协议
``runtime.execution.pretool.pipeline.AuthorizationResourceResolver`` 一致,
返回 ``(resource_type, resource_id)`` 二元组集合。缺失或非法的
``store_id`` 返回空集合,不猜测、不补全。
"""

import uuid
from collections.abc import Mapping
from typing import Any, Final


STORE_RESOURCE_TYPE: Final = "store"


class StoreResourceResolver:
    """从规范化参数提取 ``store`` 资源引用,并归一化为 UUID 字符串。"""

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> frozenset[tuple[str, str]]:
        raw = arguments.get("store_id")
        if isinstance(raw, uuid.UUID):
            candidate = str(raw)
        elif isinstance(raw, str):
            candidate = raw.strip()
        else:
            return frozenset()
        try:
            store_id = uuid.UUID(candidate)
        except ValueError:
            return frozenset()
        return frozenset({(STORE_RESOURCE_TYPE, str(store_id))})


__all__ = [
    "STORE_RESOURCE_TYPE",
    "StoreIdsResourceResolver",
    "StoreResourceResolver",
]


class StoreIdsResourceResolver:
    """从规范化参数提取多个 ``store`` 资源引用(``compare_listing_prices``)。

    非法或缺失的 ``store_ids`` 项被跳过,不猜测、不补全;返回集合按资源 ID
    去重,保证确定性。
    """

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> frozenset[tuple[str, str]]:
        raw = arguments.get("store_ids")
        if isinstance(raw, str):
            candidates: list[Any] = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            candidates = list(raw)
        else:
            return frozenset()
        resources: set[tuple[str, str]] = set()
        for value in candidates:
            try:
                store_id = uuid.UUID(str(value).strip())
            except (ValueError, TypeError):
                continue
            resources.add((STORE_RESOURCE_TYPE, str(store_id)))
        return frozenset(resources)
