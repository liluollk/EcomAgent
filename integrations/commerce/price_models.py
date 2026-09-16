"""商品调价领域契约 — 跨平台统一的调价语言。

调用链上的每一层只认这里的类型：

    Agent Tool 产出 PriceChangeCommand
      → 规则/审批读 ProductSnapshot 与 PriceConstraints
      → Adapter 把它翻译成平台请求（PriceWriteReceipt）
      → 回查产出 PriceVerification

边界规则：
  - 目标价一律 Decimal。二进制浮点在比价与回查处会留尾差，mock/真实平台
    都可能因此把「等于目标价」判成「不等于」；
  - 领域命令不含平台私有字段（签名、access token、app key、平台侧编码、
    成本价）。这些只在 Adapter/Client 内部生成或由内部 CostProvider 提供；
  - 平台做不到的能力不静默降级，返回 CAPABILITY_UNSUPPORTED；
  - 「写成功」不等于「价格已生效」：结果一律以回查（PriceVerification）为准。

PricePlatform 是调价执行面接缝：Adapter 实现它，协调器只依赖它，
因此协调器的状态机与重试/回查语义可以在假平台上被确定性地测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class PriceErrorCode(Enum):
    """统一平台错误分类。

    前四类与 execution.error_classification.ErrorCategory 同名同值，
    保证 Adapter 的归类结果能直接喂给 Execution Policy 的重试判定；
    后两类是调价闭环特有的语义：
      UNKNOWN_OUTCOME        写入请求已发出但结果未知（必须回查，不可重试写）
      CAPABILITY_UNSUPPORTED 平台不具备该能力（配置/平台选择错误，重试无意义）
    """

    CLIENT_ERROR = "CLIENT_ERROR"
    BUSINESS_ERROR = "BUSINESS_ERROR"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    FATAL_ERROR = "FATAL_ERROR"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"

    @property
    def retryable(self) -> bool:
        """只有瞬态错误可原地重试（未知结果必须先回查再决定）。"""
        return self is PriceErrorCode.TRANSIENT_ERROR


class PriceError(Exception):
    """调价链路的统一异常。

    platform / platform_code 是诊断信息，只用于日志与事件，不参与领域决策；
    它们不会出现在任何领域命令对象里。
    """

    def __init__(
        self,
        code: PriceErrorCode,
        message: str,
        *,
        platform: str = "",
        platform_code: Any = None,
        side_effect_possible: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.platform = platform
        self.platform_code = platform_code
        # True 表示「平台侧可能已经落库」——调用方必须走回查而不是重试写
        self.side_effect_possible = side_effect_possible

    @property
    def retryable(self) -> bool:
        return self.code.retryable

    def __str__(self) -> str:
        where = f"@{self.platform}" if self.platform else ""
        raw = f"(平台码 {self.platform_code})" if self.platform_code is not None else ""
        return f"[{self.code.value}]{where} {self.message}{raw}"


def as_price(value: Any, field_name: str = "price") -> Decimal:
    """把价格输入收敛为 Decimal。

    接受 Decimal / str / int；**拒绝 float**（浮点字面量是不可逆的精度陷阱，
    调用方应在边界处就给出精确表示）。bool 也拒绝——它是 int 的子类。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError(f"{field_name} 不接受 bool（请给出 Decimal 或精确字符串）")
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} 不接受 float（二进制浮点会引入尾差）："
            f"请先用 Decimal(str(value)) 或直接传精确字符串"
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} 不能为空字符串")
        try:
            return Decimal(text)
        except Exception as exc:  # noqa: BLE001 - 统一转成可解释错误
            raise ValueError(f"{field_name} 不是合法的十进制价格: {value!r}") from exc
    raise TypeError(f"{field_name} 不支持的类型: {type(value).__name__}")


def quantize_price(value: Decimal, scale: int, rounding: str = ROUND_HALF_UP) -> Decimal:
    """按平台价格精度（小数位）收敛价格。"""
    if not isinstance(value, Decimal):
        raise TypeError("quantize_price 只接受 Decimal")
    exponent = Decimal(1).scaleb(-int(scale))
    return value.quantize(exponent, rounding=rounding)


@dataclass(frozen=True)
class ProductRef:
    """商品定位 — 平台 + 店铺 + 商品 + SKU 四元组。

    平台私有编码不属于这里：Adapters 负责把 product_id/sku_id 映射到
    平台侧的 num_iid / product_id 等字段。
    """

    platform: str
    shop_id: str
    product_id: str
    sku_id: str

    def __post_init__(self) -> None:
        for name in ("platform", "shop_id", "product_id", "sku_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ProductRef.{name} 必须是非空字符串")
        object.__setattr__(self, "platform", self.platform.strip().lower())

    @property
    def key(self) -> str:
        """稳定字符串键（操作记录 / 幂等键成分）。"""
        return f"{self.platform}:{self.shop_id}:{self.product_id}:{self.sku_id}"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class ProductSnapshot:
    """查询快照 — 当前价/库存/商品状态/活动信息。

    快照是调价的决策输入，也是回查的比对基准，因此价格精度必须明确。
    """

    product_ref: ProductRef
    name: str = ""
    current_price: Decimal = Decimal("0")
    stock: int = 0
    status: str = "unknown"
    activity_name: str = ""
    activity_locked: bool = False
    observed_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_price", as_price(self.current_price, "current_price"))
        object.__setattr__(self, "stock", int(self.stock))
        object.__setattr__(self, "activity_locked", bool(self.activity_locked))


@dataclass(frozen=True)
class PriceConstraints:
    """调价约束 — 规则校验的输入。

    cost_price 来自内部 CostProvider（平台/财务持有的真相），
    绝不是模型或领域命令能提供的数据。
    """

    product_ref: ProductRef
    cost_price: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    price_scale: int = 2
    locked_by_activity: bool = False
    source: str = "cost_provider"

    def __post_init__(self) -> None:
        for name in ("cost_price", "min_price", "max_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_price(value, name))
        object.__setattr__(self, "price_scale", int(self.price_scale))


@dataclass(frozen=True)
class PriceChangeCommand:
    """领域命令 — 一次调价意图的完整描述。

    只含业务语义：谁（requester）基于什么理由（reason）要把哪个商品
    （product_ref）改到什么价（target_price）。幂等键、签名、平台编码
    都在执行层生成。
    """

    operation_id: str
    product_ref: ProductRef
    target_price: Decimal
    requester: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("PriceChangeCommand.operation_id 必须是非空字符串")
        if not isinstance(self.product_ref, ProductRef):
            raise TypeError("PriceChangeCommand.product_ref 必须是 ProductRef")
        if not isinstance(self.requester, str) or not self.requester.strip():
            raise ValueError("PriceChangeCommand.requester 必须是非空字符串")
        price = as_price(self.target_price, "target_price")
        if price <= 0:
            raise ValueError(f"target_price 必须为正数: {price}")
        object.__setattr__(self, "target_price", price)


@dataclass(frozen=True)
class PriceWriteReceipt:
    """平台写接口的即时回执 — 只是「请求被受理」的证据，不是「价格已生效」。"""

    product_ref: ProductRef
    applied_price: Decimal
    idempotent_replay: bool = False
    platform_status: str = ""
    attempts: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "applied_price", as_price(self.applied_price, "applied_price"))
        object.__setattr__(self, "attempts", int(self.attempts))


@dataclass(frozen=True)
class PriceVerification:
    """回查结论 — 调价是否真的生效的最终依据。"""

    product_ref: ProductRef
    expected_price: Decimal
    observed_price: Decimal | None
    consistent: bool
    attempts: int = 1
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_price", as_price(self.expected_price, "expected_price"))
        if self.observed_price is not None:
            object.__setattr__(
                self, "observed_price", as_price(self.observed_price, "observed_price")
            )
        object.__setattr__(self, "attempts", int(self.attempts))
        object.__setattr__(self, "consistent", bool(self.consistent))


@runtime_checkable
class PricePlatform(Protocol):
    """调价执行面 — 每个平台的 Adapter 实现这三个动作。

    契约要点：
      - query_snapshot: 读当前价/库存/状态/活动（回查也走它）
      - apply_price:     写目标价，返回回执；同一 operation_id 必须幂等
      - verify_price:    按目标价回查，返回是否一致
      - 鉴权（签名 / access token）在实现内部生成，签名不作为入参
    """

    platform: str

    async def query_snapshot(self, ref: ProductRef) -> ProductSnapshot:  # pragma: no cover
        """查询商品快照。"""
        ...

    async def apply_price(
        self,
        command: PriceChangeCommand,
        *,
        idempotency_key: str | None = None,
    ) -> PriceWriteReceipt:  # pragma: no cover
        """写入目标价（幂等键由执行层给出，重试复用同一个键）。"""
        ...

    async def verify_price(
        self,
        ref: ProductRef,
        expected_price: Decimal,
    ) -> PriceVerification:  # pragma: no cover
        """回查目标价是否生效。"""
        ...


def get_price_platform(platform: str) -> PricePlatform:
    """按平台取调价执行面。

    没有实现的平台返回 CAPABILITY_UNSUPPORTED，而不是回退到 mock ——
    平台选择错误必须在能力检查处暴露。
    """
    from integrations.commerce.adapter import get_adapter  # 延迟导入，避免循环依赖

    adapter = get_adapter(platform)
    missing = [
        name
        for name in ("query_snapshot", "apply_price", "verify_price")
        if not callable(getattr(adapter, name, None))
    ]
    if missing:
        raise PriceError(
            PriceErrorCode.CAPABILITY_UNSUPPORTED,
            f"平台 {platform} 未实现调价执行面（缺少 {', '.join(missing)}）",
            platform=platform,
        )
    return adapter  # type: ignore[return-value]


__all__ = [
    "PriceChangeCommand",
    "PriceConstraints",
    "PriceError",
    "PriceErrorCode",
    "PricePlatform",
    "PriceVerification",
    "PriceWriteReceipt",
    "ProductRef",
    "ProductSnapshot",
    "as_price",
    "get_price_platform",
    "quantize_price",
]
