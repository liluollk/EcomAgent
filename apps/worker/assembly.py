"""商品域组合根的兼容导出。

实际组装位于 :mod:`composition.product_operations`。Eval 可直接依赖组合根,
不需要导入 ``apps``；本模块仅为仍使用旧入口的调用方保留兼容导出。
"""

from composition.product_operations import (
    ADAPTER_KEY,
    BACKEND_KEY,
    MODEL_CONNECTION_KEY,
    ProductApplication,
    RunView,
    ToolCallSnapshot,
    _bootstrap_capabilities,
    build_product_application,
)


__all__ = [
    "ADAPTER_KEY",
    "BACKEND_KEY",
    "MODEL_CONNECTION_KEY",
    "ProductApplication",
    "RunView",
    "ToolCallSnapshot",
    "_bootstrap_capabilities",
    "build_product_application",
]
