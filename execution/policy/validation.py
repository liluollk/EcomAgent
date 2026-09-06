"""
ResultValidation — 上游响应校验。

HTTP 200 ≠ Tool 成功。校验分两层：
  1. Schema Validation: 响应结构是否符合预期（必填字段存在、类型正确）
  2. Business Validation: 业务语义是否合法（如价格 > 0、库存 >= 0）

契约注记：mock 平台对未知 SKU 返回「空库存行」（仅 channel/sku，缺 stock/name），
这是约定的优雅降级形状而非畸形响应——因此 query_inventory 的 stock/name
列为「可选字段」：缺省放行、出现则校验类型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ValidationError(Exception):
    """上游响应校验失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# 每个操作的期望响应 Schema（字段 → 期望类型）
_SCHEMAS: dict[str, dict[str, type | tuple]] = {
    "query_inventory": {"stock": int, "name": str},
    "update_price": {"sku": str, "new_price": (int, float)},
    "create_promotion": {"sku": str, "discount": (int, float)},
    "query_order_status": {"status": str},
    "product_shelf": {"sku": str, "action": str},
    "service_ticket": {"ticket_id": str},
    "query_order_stats": {"orders": int, "gmv": (int, float)},
    "query_anomalies": {"items": list},
    "query_promotions": {"items": list},
    "query_after_sales_stats": {"refund_rate": (int, float), "tickets": int},
    "query_knowledge_base": {"matched": bool},
}

# 允许缺省的字段（按操作）：缺省放行，出现仍校验类型
_OPTIONAL_FIELDS: dict[str, frozenset[str]] = {
    "query_inventory": frozenset({"stock", "name"}),
}


@dataclass
class ResultValidator:
    def validate(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        schema = _SCHEMAS.get(operation)
        if schema is None:
            return data
        optional = _OPTIONAL_FIELDS.get(operation, frozenset())
        for field, expected_type in schema.items():
            if field not in data:
                if field in optional:
                    continue
                raise ValidationError(
                    "UPSTREAM_INVALID_RESPONSE",
                    f"操作 {operation} 响应缺少必填字段 {field}",
                )
            value = data[field]
            if not isinstance(value, expected_type) or isinstance(value, bool) and expected_type is int:
                raise ValidationError(
                    "UPSTREAM_INVALID_RESPONSE",
                    f"操作 {operation} 字段 {field} 类型错误: "
                    f"期望 {expected_type}, 实际 {type(value).__name__}",
                )
        return data

    def validate_business(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        if operation == "update_price":
            price = data.get("new_price", 0)
            if price <= 0:
                raise ValidationError(
                    "BUSINESS_RULE_VIOLATION",
                    f"价格必须大于 0，实际: {price}",
                )
        if operation == "query_inventory":
            stock = data.get("stock")
            if stock is not None and stock < 0:
                raise ValidationError(
                    "BUSINESS_RULE_VIOLATION",
                    f"库存不能为负数，实际: {stock}",
                )
        return data
