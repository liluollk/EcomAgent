"""商品域 InputRequirementResolver 及 PreToolUse 注入协议适配(计划 03 Task 3)。

领域入口 :class:`ProductInputRequirementResolver`:

- ``resolve``:按架构文档 §5 判定关键字段(白名单:``store_id``/``sku``/
  ``target_price``,以及 P1 写目标的模型可见引用 ``store_ref``/``location_ref``)
  的 Provenance;任一关键字段缺失、歧义或来源不可信时,返回可由用户回答的
  :class:`InputRequirement`;金额格式非法时返回校验诊断而不是 Input Request。
  证据匹配候选是当前 Workspace 全部 ACTIVE 店铺(架构文档 §5 的"Workspace
  别名表"):越权店铺的用户显式证据仍会产生 USER_EXPLICIT,由 Access Control
  阶段以资源范围拒绝。``location_ref`` 的证据在**已确定的店铺作用域内**判定,
  避免不同店铺的同名"默认仓"把用户已明确的目标误判成歧义;
  ``location_ref`` / ``store_ref`` 只在**模型实际提供**时参与来源判定(缺省的
  ``location_ref`` 由 Service 确定性解析),但目标店铺已确定且存在多个启用位置
  时,缺省本身也会产生要求补充 ``location_ref`` 的 Input Request;``store_ref``
  候选项取店铺名称(``StoreReferenceResolver`` 只接受名称/别名,不接受内部 UUID);
  位置候选项只在 **Actor 授权店铺**内生成——店铺证据取自 Workspace 全量别名表,
  可能指向越权店铺,此时不生成候选项,由 Access Control 以资源范围确定性拒绝;
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
from typing import Any, Final
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
    TRUSTED_TRUST_LEVELS,
    ArgumentTrustLevel,
    EvidenceResolver,
    LocationCandidate,
    ProvenanceOutcome,
    StoreCandidate,
)
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    Product,
    Store,
)
from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import (
    ArgumentProvenance as RuntimeArgumentProvenance,
)
from runtime.execution.pretool.decisions import SchemaDiagnostic
from runtime.interfaces.requests import InputRequirement


_INVALID_PRICE_MESSAGE = "target_price 不是合法金额(需要至多两位小数的非负数值)"

#: P1 写目标的模型可见引用字段。它们与 ``store_id`` 同属"可被模型猜测的业务
#: 目标",但只在**实际提供**时才要求来源证明:``location_ref`` 可缺省,由
#: Service 在唯一启用位置时确定性解析,多位置时要求用户补充。
_P1_PROVENANCE_FIELDS: Final = ("store_ref", "location_ref")


def _question_for(field_paths: Sequence[str]) -> str:
    return "请补充以下参数:" + "、".join(field_paths)


def _store_in_actor_scope(actor: ActorContext, store_id: str) -> bool:
    """目标店铺是否在 Actor 授权范围内(``None`` = ALL,视为全部授权)。

    位置证据的匹配集是 Workspace 全量别名表,可能指向越权店铺;位置候选项与
    "多位置要求补充"都必须以授权范围为准,越权目标不生成候选项,交由 Access
    Control 以资源范围确定性拒绝。
    """
    # 调用点导入打破 ``input`` ↔ ``tools`` 的模块级环(见 references.py 说明)。
    from agents.product_operations.tools.access import authorized_store_ids

    authorized = authorized_store_ids(actor)
    return True if authorized is None else store_id in authorized


class _FrozenModel(BaseModel):
    """frozen 值模型基类;与商品域 Tool Schema 的约定一致。"""

    model_config = ConfigDict(frozen=True)


class InputOption(_FrozenModel):
    """Input Request 的单个候选项。"""

    value: str
    label: str


class ProductInputResolution(_FrozenModel):
    """``resolve`` 的返回值:Provenance 映射 + 可选 Input Request + 校验诊断。

    ``store_scope`` 是本次调用已确定的规范 ``str(store.id)``(来源为 ``store_id``
    或 ``store_ref`` 之一);供适配层在**诊断驱动**路径上按目标店铺补齐位置候选。
    """

    requirement: InputRequirement | None = None
    provenance: Mapping[str, ArgumentTrustLevel]
    normalized_arguments: Mapping[str, Any]
    diagnostics: tuple[SchemaDiagnostic, ...] = ()
    store_scope: str | None = None


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
        fields: Sequence[str] = KEY_ARGUMENT_FIELDS,
    ) -> ProductInputResolution:
        """判定关键字段来源;缺失/歧义/不可信 → Input,金额非法 → 诊断。

        ``fields`` 是当前 Tool 的字段集合(由 PreToolProfile 给出),决定归一化
        白名单与"关键字段"判定范围;只读 Tool 的字段不在写字段集合内,因此
        不产生 Input Requirement。

        证据匹配候选为当前 Workspace 全部 ACTIVE 店铺(架构文档 §5 的
        "Workspace 别名表");授权范围只缩窄 Input Request 候选项,不缩窄
        证据匹配——越权店铺的用户显式证据产生 USER_EXPLICIT 后由 Access
        Control 以资源范围拒绝,而不是退回"无证据"。``location_ref`` 的证据先
        按已确定的店铺作用域过滤候选;目标店铺已确定、属于 Actor 授权范围、
        店铺内有多个启用位置、又未给出 ``location_ref`` 时,直接产生要求补充
        该字段的 Input Request(越权店铺不生成候选项,交由 Access Control 拒绝)。
        """
        normalized = normalize_arguments(dict(arguments), fields=fields)
        stores: tuple[StoreCandidate, ...] = ()
        locations: tuple[LocationCandidate, ...] = ()
        if actor is not None:
            stores = await self._load_workspace_store_candidates(actor)
            locations = await self._load_location_candidates(actor)
        sku_value = normalized.get("sku")
        sku_unique: bool | None = None
        if actor is not None and isinstance(sku_value, str) and sku_value:
            sku_unique = await self._sku_is_unique(actor, sku_value)

        outcome = EvidenceResolver().resolve(
            user_message=user_message,
            arguments=normalized,
            stores=stores,
            locations=locations,
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
            key_fields = tuple(field for field in fields if field in KEY_ARGUMENT_FIELDS)
            # P1 写目标只在模型实际提供时才要求来源证明:location_ref 可缺省,
            # 由 Service 在唯一启用位置时确定性解析(多位置由 Service 要求补充)。
            provided_provenance = tuple(
                field
                for field in fields
                if field in _P1_PROVENANCE_FIELDS and field in normalized
            )
            untrusted_fields = [
                field
                for field in (*key_fields, *provided_provenance)
                if outcome.field_sources.get(field) not in TRUSTED_TRUST_LEVELS
            ]
            # 目标店铺已确定、工具声明了 location_ref、却未给出且该店铺有多个
            # 启用位置:在 PreToolUse 就直接要求补充,不必等 Handler 才确定性失败。
            # 越权店铺不在此列:让 Access Control 以资源范围确定性拒绝。
            if self._requires_location_choice(
                fields, normalized, outcome, locations, actor=actor
            ):
                untrusted_fields.append("location_ref")
            untrusted = tuple(dict.fromkeys(untrusted_fields))
            if untrusted:
                requirement = await self.requirement_for(
                    untrusted, actor=actor, store_scope=outcome.store_scope
                )
        return ProductInputResolution(
            requirement=requirement,
            provenance=dict(outcome.field_sources),
            normalized_arguments=normalized_arguments,
            diagnostics=diagnostics,
            store_scope=outcome.store_scope,
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

    async def store_reference_options(self, actor: ActorContext) -> tuple[InputOption, ...]:
        """``store_ref`` 的候选项(值为店铺**名称**,可被 store_ref 解析)。

        与 :meth:`store_options` 的差异只在取值:P1 Tool 的模型可见字段是
        ``store_ref``,而 :class:`StoreReferenceResolver` 只接受名称/别名,
        不接受内部 UUID,因此候选项不能给出 ``str(store.id)``。
        """
        candidates = await self._load_store_candidates(actor)
        return tuple(
            InputOption(value=candidate.name, label=candidate.name)
            for candidate in candidates
        )

    async def _load_store_candidates(
        self, actor: ActorContext
    ) -> tuple[StoreCandidate, ...]:
        """授权店铺候选(Input Request 选项专用;严格限定 Actor 范围)。"""
        # 调用点导入打破 ``input`` ↔ ``tools`` 的模块级环(见 references.py 说明)。
        from agents.product_operations.tools.access import authorized_store_ids

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

    async def _load_location_candidates(
        self, actor: ActorContext
    ) -> tuple[LocationCandidate, ...]:
        """证据匹配候选:当前 Workspace 全部 ACTIVE 履约位置(不做授权过滤)。

        位置证据只在提供 ``location_ref`` 时使用;越权位置仍由 Service 的
        授权解析拒绝,证据匹配与授权判定分工不合并。
        """
        stmt = (
            select(FulfillmentLocation)
            .where(
                FulfillmentLocation.workspace_id == actor.workspace_id,
                FulfillmentLocation.status == StoreStatus.ACTIVE,
            )
            .order_by(FulfillmentLocation.name, FulfillmentLocation.id)
        )
        rows = (await self._session.scalars(stmt)).all()
        return tuple(
            LocationCandidate(
                location_id=str(location.id),
                store_id=str(location.store_id),
                name=location.name,
                aliases=tuple(location.aliases),
            )
            for location in rows
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

    async def requirement_for(
        self,
        field_paths: tuple[str, ...],
        *,
        actor: ActorContext | None,
        store_scope: str | None = None,
    ) -> InputRequirement:
        """为需要补充的字段构造 Input Request,并补齐服务端候选项。

        候选项全部来自 Actor 授权范围:``store_id`` / ``store_ref`` 走授权过滤的
        候选加载;``location_ref`` 只在 ``store_scope`` **属于授权店铺**时才生成
        ——店铺证据取自 Workspace 全量别名表,可能指向越权店铺,此时不生成位置
        候选(避免泄露资源信息),交由 Access Control 确定性拒绝。
        """
        options: dict[str, Any] = {}
        if "store_id" in field_paths and actor is not None:
            store_options = await self.store_options(actor)
            if store_options:
                options["store_id"] = tuple(
                    option.model_dump() for option in store_options
                )
        if "store_ref" in field_paths and actor is not None:
            ref_options = await self.store_reference_options(actor)
            if ref_options:
                options["store_ref"] = tuple(
                    option.model_dump() for option in ref_options
                )
        if (
            "location_ref" in field_paths
            and actor is not None
            and store_scope is not None
            and _store_in_actor_scope(actor, store_scope)
        ):
            location_options = await self._location_options(actor, store_scope)
            if location_options:
                options["location_ref"] = tuple(
                    option.model_dump() for option in location_options
                )
        return InputRequirement(
            field_paths=field_paths,
            question=_question_for(field_paths),
            response_schema={},
            options=options,
        )

    @staticmethod
    def _requires_location_choice(
        fields: Sequence[str],
        normalized: Mapping[str, Any],
        outcome: ProvenanceOutcome,
        locations: Sequence[LocationCandidate],
        *,
        actor: ActorContext | None,
    ) -> bool:
        """需在 PreToolUse 直接要求补充 ``location_ref`` 的场景。

        四个条件同时成立:工具声明了 ``location_ref``;模型没有给出该字段;
        目标店铺已确定(``store_scope`` 可信)**且属于 Actor 授权范围**;该店铺
        存在**多个**启用位置。此时唯一位置无法确定性解析,提前要求补充优于进入
        Handler 后才失败。越权店铺不在此列:交由 Access Control 确定性拒绝,
        既不生成位置候选,也不向用户追问无权操作的目标。
        """
        if "location_ref" not in fields or "location_ref" in normalized:
            return False
        if outcome.store_scope is None:
            return False
        if actor is None or not _store_in_actor_scope(actor, outcome.store_scope):
            return False
        return sum(loc.store_id == outcome.store_scope for loc in locations) > 1

    async def _location_options(
        self, actor: ActorContext, store_scope: str
    ) -> tuple[InputOption, ...]:
        """目标店铺内启用位置的候选项(值为位置**名称**,可被 ``location_ref`` 解析)。"""
        candidates = await self._load_location_candidates(actor)
        return tuple(
            InputOption(value=candidate.name, label=candidate.name)
            for candidate in candidates
            if candidate.store_id == store_scope
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
    """``InputNormalizer`` 适配:按当前 Tool 字段集合归一化(含店铺别名→ID)。"""

    def __init__(
        self,
        resolver: ProductInputRequirementResolver,
        *,
        fields: Sequence[str],
        user_message: str,
        actor: ActorContext,
        answered: Mapping[str, str] | None = None,
    ) -> None:
        self._resolver = resolver
        self._fields = tuple(fields)
        self._user_message = user_message
        self._actor = actor
        self._answered = answered

    async def normalize(self, *, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        resolution = await self._resolver.resolve(
            user_message=self._user_message,
            arguments=arguments,
            actor=self._actor,
            answered=self._answered,
            fields=self._fields,
        )
        return resolution.normalized_arguments


class ProductPreToolProvenanceResolver:
    """``ArgumentProvenanceResolver`` 协议适配。

    runtime 协议只注入 ``arguments``,当前用户消息/结构化回答在构造时绑定;
    返回 runtime 的 ``ArgumentProvenance`` 值对象(field_sources 直接携带
    Runtime 的 :class:`ArgumentTrustLevel`,不再回退到字符串)。
    """

    def __init__(
        self,
        resolver: ProductInputRequirementResolver,
        *,
        user_message: str,
        actor: ActorContext | None = None,
        answered: Mapping[str, str] | None = None,
        history: Sequence[str] = (),
        fields: Sequence[str] = KEY_ARGUMENT_FIELDS,
    ) -> None:
        self._resolver = resolver
        self._user_message = user_message
        self._actor = actor
        self._answered = answered
        self._history = history
        self._fields = tuple(fields)

    async def resolve(self, *, arguments: Mapping[str, Any]) -> RuntimeArgumentProvenance:
        resolution = await self._resolver.resolve(
            user_message=self._user_message,
            arguments=arguments,
            actor=self._actor,
            answered=self._answered,
            history=self._history,
            fields=self._fields,
        )
        return RuntimeArgumentProvenance(
            field_sources=dict(resolution.provenance)
        )


class ProductPreToolInputRequirementResolver:
    """``InputRequirementResolver`` 协议适配;两条来源:

    - **诊断驱动**(``diagnostic`` 非空):把 SchemaDiagnostic 命中的字段转成
      Input Request,并补齐店铺/店铺引用候选项;诊断含 ``location_ref`` 时按
      目标店铺补齐位置候选。
    - **业务输入需求**(``diagnostic`` 为空):Schema 可选、却是执行前提的字段
      (多位置店铺未指定 ``location_ref``)由域层按**规范化参数**判定。此处复用
      同一个 :class:`ProductInputRequirementResolver` 的判定结果,不重复实现规则。

    只有声明了 ``location_ref`` 的工具会在无诊断时触发业务判定,避免给其余
    Tool 增加无谓的权威读取。
    """

    def __init__(
        self,
        resolver: ProductInputRequirementResolver,
        *,
        fields: Sequence[str] = KEY_ARGUMENT_FIELDS,
        actor: ActorContext | None = None,
        user_message: str = "",
        answered: Mapping[str, str] | None = None,
        history: Sequence[str] = (),
    ) -> None:
        self._resolver = resolver
        self._fields = tuple(fields)
        self._actor = actor
        self._user_message = user_message
        self._answered = answered
        self._history = tuple(history)

    async def resolve(
        self, *, diagnostic: SchemaDiagnostic | None, arguments: Mapping[str, Any]
    ) -> InputRequirement | None:
        if diagnostic is not None:
            field_paths = tuple(
                field for field in self._fields if field in diagnostic.field_paths
            )
            if not field_paths:
                return None
            store_scope: str | None = None
            if "location_ref" in field_paths and self._actor is not None:
                resolution = await self._resolve_domain(arguments)
                store_scope = resolution.store_scope
            return await self._resolver.requirement_for(
                field_paths, actor=self._actor, store_scope=store_scope
            )
        # 无 SchemaDiagnostic:只有"执行前提缺失但 Schema 可选"的业务字段需要补充。
        # 提前短路,避免给不涉及位置的 Tool 增加权威读取。
        if (
            "location_ref" not in self._fields
            or "location_ref" in arguments
            or self._actor is None
        ):
            return None
        resolution = await self._resolve_domain(arguments)
        return resolution.requirement

    async def _resolve_domain(
        self, arguments: Mapping[str, Any]
    ) -> ProductInputResolution:
        """调用域层判定,复用与归一化阶段相同的证据规则与店铺作用域。"""
        return await self._resolver.resolve(
            user_message=self._user_message,
            arguments=arguments,
            actor=self._actor,
            answered=self._answered,
            history=self._history,
            fields=self._fields,
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
