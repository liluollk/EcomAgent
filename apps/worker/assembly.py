"""商品域运行面门面兼容导出(计划 05 Task 2)。

组合核心已下沉到 :mod:`adapters.commerce.product_runtime`:Eval Runner 需要
与 Worker 相同的商品域组装,而 eval 不得 import apps(方向:eval → runtime/
agents/adapters)。为保持单一事实源,组装核心作为整体下沉到 adapters 层
(adapters 已依赖 agents:``mock_adapter`` 即先例);本模块保留原导入路径,
供 apps/api、apps/worker 与既有测试按原样导入。
"""

from adapters.commerce.product_runtime import (
    ADAPTER_KEY,
    BACKEND_KEY,
    MODEL_CONNECTION_KEY,
    ProductRuntime,
    RunView,
    ToolCallSnapshot,
    _bootstrap_capabilities,
    build_product_runtime,
)


__all__ = [
    "ADAPTER_KEY",
    "BACKEND_KEY",
    "MODEL_CONNECTION_KEY",
    "ProductRuntime",
    "RunView",
    "ToolCallSnapshot",
    "_bootstrap_capabilities",
    "build_product_runtime",
]
