"""只读商品 Tool 的确定性错误(计划 03 Task 2)。"""


class ToolAccessError(Exception):
    """权限或资源范围拒绝;``reason_code`` 与 Runtime 授权原因码一致。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code


class ProductNotFoundError(LookupError):
    """目标 Workspace/店铺下不存在该 SKU 的上架记录。"""


__all__ = ["ProductNotFoundError", "ToolAccessError"]
