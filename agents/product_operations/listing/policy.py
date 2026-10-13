"""Listing 发布状态策略(P1 Task 8;设计 §4.5、§5.2、§6)。

设计 §5.2 要求 Policy 组件提供**预检**与**执行校验**两个入口,并共享同一份
版本化配置,避免复制规则。刊登规则固定覆盖:

1. 平台适配边界支持刊登发布(否则 ``CAPABILITY_UNSUPPORTED``,不得降级为
   "删除商品"或"库存清零"这类替代动作);
2. 取消发布原因只能是受控枚举(自由文本无法审计与回放);
3. Listing 存在(跨 Workspace 不可见);
4. 目标发布状态属于当前状态允许的迁移集合。

状态机是**显式**的:``PUBLISHED`` 与 ``UNPUBLISHED`` 互相可达,且允许"目标与
当前相同"——同目标重放是幂等 no-op(``APPLIED`` + ``changed=False``),不是错误。
允许集合写成数据而不是散落的 ``if``,后续收紧时改一处即可。

任何失败返回确定性 ``reason_code`` 的 denied 决策,不抛异常;规则所需一切来自
结构化输入与 PostgreSQL Session,不读取 Prompt、Memory 或任何文档。
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.listing.models import UnpublishReason
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.shared.errors import CAPABILITY_UNSUPPORTED
from runtime.execution.pretool.decisions import PolicyDecision


LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
INVALID_REASON: Final = "INVALID_REASON"
INVALID_STATE_TRANSITION: Final = "INVALID_STATE_TRANSITION"

_DENY_MESSAGES: Final[dict[str, str]] = {
    CAPABILITY_UNSUPPORTED: "platform adapter does not support listing publication",
    LISTING_NOT_FOUND: "listing does not exist in this workspace",
    INVALID_REASON: "unpublish reason is not a controlled value",
    INVALID_STATE_TRANSITION: "target publication state is not reachable",
}

_VALID_REASONS: Final[frozenset[str]] = frozenset(
    reason.value for reason in UnpublishReason
)

#: 状态机(设计 §4.5):双向迁移 + 同目标幂等 no-op。
_ALLOWED_TARGETS: Final[dict[ListingStatus, frozenset[ListingStatus]]] = {
    ListingStatus.PUBLISHED: frozenset(
        {ListingStatus.PUBLISHED, ListingStatus.UNPUBLISHED}
    ),
    ListingStatus.UNPUBLISHED: frozenset(
        {ListingStatus.PUBLISHED, ListingStatus.UNPUBLISHED}
    ),
}


class ListingPolicyConfig(BaseModel):
    """刊登业务配置:结构化注入,不来自 Prompt/Memory(架构文档 §6 末条)。

    ``supports_publication`` 表达当前平台适配边界是否具备刊登发布能力。P1 的
    REST Adapter 只声明定价能力,``False`` 时策略确定性拒绝
    (``CAPABILITY_UNSUPPORTED``),而不是让写入走到 Adapter 才失败,更不允许
    用"删除商品"或"库存清零"代替下架。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str
    supports_publication: bool = True


class ListingPolicyDecision(PolicyDecision):
    """刊登 Policy 决策;允许时携带当前状态与资源版本,拒绝时携带确定性原因码。"""

    policy_version: str
    current_state: ListingStatus | None = None
    listing_version: int | None = None


class ListingPolicy:
    """刊登业务策略:只读 PostgreSQL,返回 ListingPolicyDecision 值对象。"""

    def __init__(self, session: AsyncSession, config: ListingPolicyConfig) -> None:
        self._session = session
        self._config = config
        self._repository = ProductRepository(session)

    @property
    def policy_version(self) -> str:
        return self._config.policy_version

    async def evaluate(
        self,
        *,
        workspace_id: uuid.UUID,
        listing_id: uuid.UUID,
        target_state: ListingStatus,
        reason: str | None,
    ) -> ListingPolicyDecision:
        """按固定次序校验发布状态变更;全部通过返回 allowed 决策。"""
        if not self._config.supports_publication:
            return self._deny(CAPABILITY_UNSUPPORTED)
        if reason is not None and reason not in _VALID_REASONS:
            return self._deny(INVALID_REASON)

        listing = await self._repository.get_listing(workspace_id, listing_id)
        if listing is None:
            return self._deny(LISTING_NOT_FOUND)

        if target_state not in _ALLOWED_TARGETS[listing.status]:
            return self._deny(
                INVALID_STATE_TRANSITION,
                current_state=listing.status,
                listing_version=listing.version,
            )
        return ListingPolicyDecision(
            allowed=True,
            policy_version=self._config.policy_version,
            current_state=listing.status,
            listing_version=listing.version,
        )

    def _deny(
        self,
        reason_code: str,
        *,
        current_state: ListingStatus | None = None,
        listing_version: int | None = None,
    ) -> ListingPolicyDecision:
        return ListingPolicyDecision(
            allowed=False,
            reason_code=reason_code,
            safe_message=_DENY_MESSAGES[reason_code],
            policy_version=self._config.policy_version,
            current_state=current_state,
            listing_version=listing_version,
        )


__all__ = [
    "CAPABILITY_UNSUPPORTED",
    "INVALID_REASON",
    "INVALID_STATE_TRANSITION",
    "LISTING_NOT_FOUND",
    "ListingPolicy",
    "ListingPolicyConfig",
    "ListingPolicyDecision",
]
