"""Catalog 能力的输入/输出模型(P1 Task 5;设计 §4.2)。

``search_products`` 只接收 ``query`` 与 ``store_refs`` 两个模型可见字段:
内部 ``store_id`` / ``product_id`` 由服务端解析,不出现在输入 Schema。输出
同样只暴露模型可见引用(店铺名/别名),不返回成本价、平台凭证或内部 UUID。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    """frozen 值模型基类:结果对象不可变,只暴露白名单字段。"""

    model_config = ConfigDict(frozen=True)


class SearchProductsInput(_FrozenModel):
    """search_products 输入:关键词(匹配 SKU/标题)与可选店铺引用集合。"""

    query: str | None = Field(
        default=None,
        description="用户当前消息中的 SKU、商品名或搜索关键词。",
    )
    store_refs: tuple[str, ...] = Field(
        default=(),
        description=(
            "用户当前消息明确给出的店铺名称或别名;未限定时留空表示当前"
            "授权范围内的全部店铺。"
        ),
    )


class ProductStoreRef(_FrozenModel):
    """上架店铺引用(模型可见:店铺名称 + 平台)。"""

    store_ref: str
    platform: str


class ProductSearchItem(_FrozenModel):
    """search_products 单个商品条目。"""

    sku: str
    title: str
    stores: tuple[ProductStoreRef, ...] = ()


class ProductSearchResult(_FrozenModel):
    """search_products 结果。"""

    items: tuple[ProductSearchItem, ...] = ()


__all__ = [
    "ProductSearchItem",
    "ProductSearchResult",
    "ProductStoreRef",
    "SearchProductsInput",
]
