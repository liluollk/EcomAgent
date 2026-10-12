"""商品 Tool 的 PreToolProfile 行为测试(计划 04 Task 1/3)。

覆盖两条线:

- 参数线:每个 Tool 只归一化自己的字段集合,只读 Tool 的 query/store_ids
  不会被写字段白名单吞掉;
- 策略/资源线:每个 ToolBinding 声明独立的业务策略与资源解析器,写 Tool
  漏配策略在启动期确定性失败。

Runtime 路径断言复用 ``product_application`` 夹具(真库 + 脚本模型),
不直接调用 Handler 函数,确保走完整 PreToolUse 管线。
"""

from __future__ import annotations

from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
)
from runtime.backend.protocol import BackendCompleted, TextDelta, ToolCallCompleted


async def test_compare_listing_prices_normalization_preserves_sku_and_store_ids(
    product_application,
) -> None:
    """经真实 Runtime 路径:compare_listing_prices 的 sku/store_ids 不丢失。"""
    backend = product_application.scripted_backend
    store_id = str(product_application.seed_ids.store_id)
    backend.set_rounds(
        [
            [
                ToolCallCompleted(
                    call_id="compare-1",
                    name=COMPARE_LISTING_PRICES_DEFINITION.stable_key,
                    arguments={"sku": "SKU-001", "store_ids": [store_id]},
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [
                TextDelta(text_delta="已完成比价。"),
                BackendCompleted(finish_reason="stop"),
            ],
        ]
    )

    view = await product_application.start("比较授权店铺内 SKU-001 的价格。")
    snapshot = await product_application.tool_call_snapshot(view.run_id)

    assert snapshot is not None
    assert snapshot.normalized_arguments == {
        "sku": "SKU-001",
        "store_ids": [store_id],
    }
