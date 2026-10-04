"""商品域 InputRequirementResolver 及 PreToolUse 注入协议适配(计划 03 Task 3)。

领域入口 :class:`ProductInputRequirementResolver`:

- ``resolve``:按架构文档 §5 判定关键字段(白名单:``store_id``/``sku``/
  ``target_price``)的 Provenance;任一关键字段缺失、歧义或来源不可信时,
  返回可由用户回答的 :class:`InputRequirement`;金额格式非法时返回校验
  诊断而不是 Input Request。证据匹配候选是当前 Workspace 全部 ACTIVE
  店铺(架构文档 §5 的"Workspace 别名表"):越权店铺的用户显式证据仍会
  产生 USER_EXPLICIT,由 Access Control 阶段以资源范围拒绝;
- ``store_requirement``:生成店铺选择需求,候选项严格限定在 Actor 授权
  店铺内(店铺标识 = ``str(store.id)``,与 Task 2 只读 Tool 约定一致)。

:class:`ProductPreToolNormalizer` / :class:`ProductPreToolProvenanceResolver` /
:class:`ProductPreToolInputRequirementResolver` 把上述能力适配成
``runtime.execution.pretool.pipeline`` 的注入协议(计划 02 Task 6);协议
签名是异步的,因此领域入口也是异步。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StoreStatus
from agents.product_operations.input.normalizer import (
    KEY_ARGUMENT_FIELDS,
    normalize_arguments,
)
from agents.product_operations.input.provenance import (
    TRUSTED_PROVENANCE,
    ArgumentProvenance,
    EvidenceResolver,
    StoreCandidate,
)
from agents.product_operations.persistence.models import Product, Store
from agents.product_operations.tools.access import authorized_store_ids
from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import (
    ArgumentProvenance as RuntimeArgumentProvenance,
)
from runtime.execution.pretool.decisions import SchemaDiagnostic
from runtime.interfaces.requests import InputRequirement


_INVALID_PRICE_MESSAGE = "target_price 不是合法金额(需要至多两位小数的非负数值)"


def _question_for(field_paths: Sequence[str]) -> str:
    return "请补充以下参数:" + "、".join(field_paths)


class _FrozenModel(BaseModel):
    """frozen 值模型基类;与商品域 Tool Schema 的约定一致。"""

    model_config = ConfigDict(frozen=True)


class InputOption(_FrozenModel):
    """Input Request 的单个候选项。"""

    value: str
    label: str


class ProductInputResolution(_FrozenModel):
    """``resolve`` 的返回值:Provenance 映射 + 可选 Input Request + 校验诊断。"""

    requirement: InputRequirement | None = None
    provenance: Mapping[str, ArgumentProvenance]
    normalized_arguments: Mapping[str, Any]
    diagnostics: tuple[SchemaDiagnostic, ...] = ()


class StoreRequirement(_FrozenModel):
    """``store_requirement`` 的返回值:Input Request 与授权店铺候选项。"""

    requirement: InputRequirement
    options: tuple[InputOption, ...]


class ProductInputRequirementResolver:
    """商品域的 InputRequirementResolver(绑定 Session 的权威读取)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self,
        *,
        user_message: str,
        arguments: Mapping[str, Any],
        actor: ActorContext | None = None,
        answered: Mapping[str, str] | None = None,
        history: Sequence[str] = (),
    ) -> ProductInputResolution:
        """判定关键字段来源;缺失/歧义/不可信 → Input,金额非法 → 诊断。

        证据匹配候选为当前 Workspace 全部 ACTIVE 店铺(架构文档 §5 的
        "Workspace 别名表");授权范围只缩窄 Input Request 候选项,不缩窄
        证据匹配——越权店铺的用户显式证据产生 USER_EXPLICIT 后由 Access
        Control 以资源范围拒绝,而不是退回"无证据"。
        """
        normalized = normalize_arguments(dict(arguments))
        stores: tuple[StoreCandidate, ...] = ()
        if actor is not None:
            stores = await self._load_workspace_store_candidates(actor)
        sku_value = normalized.get("sku")
        sku_unique: bool | None = None
        if actor is not None and isinstance(sku_value, str) and sku_value:
            sku_unique = await self._sku_is_unique(actor, sku_value)

        outcome = EvidenceResolver().resolve(
            user_message=user_message,
            arguments=normalized,
            stores=stores,
            answered=answered,
            sku_unique_in_workspace=sku_unique,
            history=history,
        )
        normalized_arguments = {
            **normalized,
            **{key: value for key, value in outcome.canonical_values.items() if value is not None},
        }
        diagnostics = self._amount_diagnostics(normalized)

        requirement: InputRequirement | None = None
        if not diagnostics:
            untrusted = tuple(
                field
                for field in KEY_ARGUMENT_FIELDS
                if outcome.field_sources[field] not in TRUSTED_PROVENANCE
            )
            if untrusted:
                requirement = await self._build_requirement(untrusted, actor=actor)
        return ProductInputResolution(
            requirement=requirement,
            provenance=dict(outcome.field_sources),
            normalized_arguments=normalized_arguments,
            diagnostics=diagnostics,
        )

    async def store_requirement(self, actor: ActorContext) -> StoreRequirement:
        """店铺选择需求;候选项恰为 Actor 授权范围内的 ACTIVE 店铺。"""
        options = await self.store_options(actor)
        requirement = InputRequirement(
            field_paths=("store_id",),
            question="请确认要操作的店铺",
            response_schema={},
            options={"store_id": tuple(option.model_dump() for option in options)},
        )
        return StoreRequirement(requirement=requirement, options=options)

    async def store_options(self, actor: ActorContext) -> tuple[InputOption, ...]:
        """授权店铺候选项;按店铺 ID 排序保证确定性。"""
        candidates = await self._load_store_candidates(actor)
        return tuple(
            InputOption(value=candidate.store_id, label=candidate.name)
            for candidate in candidates
        )

    async def _load_store_candidates(
        self, actor: ActorContext
    ) -> tuple[StoreCandidate, ...]:
        """授权店铺候选(Input Request 选项专用;严格限定 Actor 范围)。"""
        authorized = authorized_store_ids(actor)
        stmt = (
            select(Store)
            .where(
                Store.workspace_id == actor.workspace_id,
                Store.status == StoreStatus.ACTIVE,
            )
            .order_by(Store.id)
        )
        if authorized is not None:
            if not authorized:
                return ()
            stmt = stmt.where(Store.id.in_([UUID(value) for value in sorted(authorized)]))
        rows = (await self._session.scalars(stmt)).all()
        return tuple(
            StoreCandidate(
                store_id=str(store.id), name=store.name, aliases=tuple(store.aliases)
            )
            for store in rows
        )

    async def _load_workspace_store_candidates(
        self, actor: ActorContext
    ) -> tuple[StoreCandidate, ...]:
        """证据匹配候选:当前 Workspace 全部 ACTIVE 店铺(架构文档 §5 的
        "Workspace 别名表");不做授权过滤,越权引用由 Access Control 拒绝。"""
        stmt = (
            select(Store)
            .where(
                Store.workspace_id == actor.workspace_id,
                Store.status == StoreStatus.ACTIVE,
            )
            .order_by(Store.id)
        )
        rows = (await self._session.scalars(stmt)).all()
        return tuple(
            StoreCandidate(
                store_id=str(store.id), name=store.name, aliases=tuple(store.aliases)
            )
            for store in rows
        )

    async def _sku_is_unique(self, actor: ActorContext, sku: str) -> bool:
        """权威读取:SKU 在当前 Workspace 是否恰好唯一(唯一约束保证 ≤ 1)。"""
        ids = (
            await self._session.scalars(
                select(Product.id).where(
                    Product.workspace_id == actor.workspace_id,
                    Product.sku == sku,
                )
            )
        ).all()
        return len(ids) == 1

    async def _build_requirement(
        self, field_paths: tuple[str, ...], *, actor: ActorContext | None
    ) -> InputRequirement:
        options: dict[str, Any] = {}
        if "store_id" in field_paths and actor is not None:
            store_options = await self.store_options(actor)
            if store_options:
                options["store_id"] = tuple(
                    option.model_dump() for option in store_options
                )
        return InputRequirement(
            field_paths=field_paths,
            question=_question_for(field_paths),
            response_schema={},
            options=options,
        )

    @staticmethod
    def _amount_diagnostics(normalized: Mapping[str, Any]) -> tuple[SchemaDiagnostic, ...]:
        price = normalized.get("target_price")
        if "target_price" in normalized and not isinstance(price, Decimal):
            return (
                SchemaDiagnostic(field_paths=("target_price",), message=_INVALID_PRICE_MESSAGE),
            )
        return ()


# ---- runtime PreToolUse 注入协议适配(计划 02 Task 6) ----


class ProductPreToolNormalizer:
    """``InputNormalizer`` 协议适配:确定性归一化,不做模型猜测。"""

    async def normalize(self, *, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return normalize_arguments(dict(arguments))


class ProductPreToolProvenanceResolver:
    """``ArgumentProvenanceResolver`` 协议适配。

    runtime 协议只注入 ``arguments``,当前用户消息/结构化回答在构造时绑定;
    返回 runtime 的 ``ArgumentProvenance`` 值对象(field_sources 为来源字符串)。
    """

    def __init__(
        self,
        resolver: ProductInputRequirementResolver,
        *,
        user_message: str,
        actor: ActorContext | None = None,
        answered: Mapping[str, str] | None = None,
        history: Sequence[str] = (),
    ) -> None:
        self._resolver = resolver
        self._user_message = user_message
        self._actor = actor
        self._answered = answered
        self._history = history

    async def resolve(self, *, arguments: Mapping[str, Any]) -> RuntimeArgumentProvenance:
        resolution = await self._resolver.resolve(
            user_message=self._user_message,
            arguments=arguments,
            actor=self._actor,
            answered=self._answered,
            history=self._history,
        )
        return RuntimeArgumentProvenance(
            field_sources={
                field: level.value for field, level in resolution.provenance.items()
            }
        )


class ProductPreToolInputRequirementResolver:
    """``InputRequirementResolver`` 协议适配:SchemaDiagnostic → InputRequirement。"""

    def __init__(
        self,
        resolver: ProductInputRequirementResolver,
        *,
        actor: ActorContext | None = None,
    ) -> None:
        self._resolver = resolver
        self._actor = actor

    async def resolve(self, *, diagnostic: SchemaDiagnostic) -> InputRequirement | None:
        field_paths = tuple(
            field for field in KEY_ARGUMENT_FIELDS if field in diagnostic.field_paths
        )
        if not field_paths:
            return None
        options: dict[str, Any] = {}
        if "store_id" in field_paths and self._actor is not None:
            store_options = await self._resolver.store_options(self._actor)
            if store_options:
                options["store_id"] = tuple(
                    option.model_dump() for option in store_options
                )
        return InputRequirement(
            field_paths=field_paths,
            question=_question_for(field_paths),
            response_schema={},
            options=options,
        )


__all__ = [
    "InputOption",
    "ProductInputRequirementResolver",
    "ProductInputResolution",
    "ProductPreToolInputRequirementResolver",
    "ProductPreToolNormalizer",
    "ProductPreToolProvenanceResolver",
    "StoreRequirement",
]
