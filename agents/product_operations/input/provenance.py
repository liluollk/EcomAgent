"""参数来源(ArgumentTrustLevel)的确定性证据规则(计划 03 Task 3;架构文档 §5)。

Provenance 只由四类白名单证据产生,其余一律 ``UNTRUSTED``:

- ``USER_EXPLICIT``:当前用户消息中的精确证据(店铺别名/SKU 在大小写与
  空白归一后精确出现;无模糊、无语义匹配),并经 Workspace 别名表归一化;
- ``USER_CONFIRMED``:已处理 Input Request 的结构化回答;
- ``SYSTEM_RESOLVED``:当前 Workspace 权威数据的唯一匹配(SKU 全工作区唯一);
- ``DETERMINISTIC_DERIVED``:由明确规则确定性计算(合法金额的规范化)。

可信等级类型由 Runtime 唯一定义(:mod:`runtime.execution.pretool.provenance`),
本模块只做商品字段的证据判定,不再自带同名枚举。

明确不受信:旧对话内容(传入 ``history`` 即被忽略)、模型自报的
``source``/``confidence``(归一化白名单丢弃)、"只有一家授权店"、歧义别名、
无匹配的 SKU 与非法金额。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agents.product_operations.input.normalizer import (
    normalize_amount,
    normalize_label,
)
from runtime.execution.pretool.provenance import (
    TRUSTED_TRUST_LEVELS,
    ArgumentTrustLevel,
)


@dataclass(frozen=True)
class StoreCandidate:
    """参与证据匹配的授权店铺(id 为 ``str(store.id)``,与 Tool 约定一致)。"""

    store_id: str
    name: str
    aliases: tuple[str, ...] = ()

    def matches_value(self, value: str) -> bool:
        """模型提供的店铺标识是否指向本店(id 或归一化后的名称/别名)。"""
        return _candidate_matches_value(self.store_id, self._labels(), value)

    def matched_by_message(self, normalized_message: str) -> bool:
        """归一化消息中是否精确出现本店的名称/别名。"""
        return bool(self.matched_labels(normalized_message))

    def matched_labels(self, normalized_message: str) -> frozenset[str]:
        """返回当前消息实际命中的归一化名称/别名。"""
        return _candidate_matched_labels(self._labels(), normalized_message)

    def normalized_labels(self) -> frozenset[str]:
        """返回本店非空的归一化名称/别名集合。"""
        return _candidate_normalized_labels(self._labels())

    def _labels(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class LocationCandidate:
    """参与证据匹配的启用履约位置(id 为 ``str(location.id)``)。

    P1 写 Tool 用 ``location_ref`` 引用仓库/门店位置。多仓场景下模型可能自行
    补一个合法位置来绕过"多位置必须补充输入",因此 ``location_ref`` 的提供值
    同样需要来源证明(见 :class:`EvidenceResolver`);缺省值不由本类判定,
    交给 Service 在唯一启用位置时确定性解析。
    """

    location_id: str
    store_id: str
    name: str
    aliases: tuple[str, ...] = ()

    def matches_value(self, value: str) -> bool:
        """模型提供的位置标识是否指向本位置(id 或归一化后的名称/别名)。"""
        return _candidate_matches_value(self.location_id, self._labels(), value)

    def matched_labels(self, normalized_message: str) -> frozenset[str]:
        """返回当前消息实际命中的归一化名称/别名。"""
        return _candidate_matched_labels(self._labels(), normalized_message)

    def normalized_labels(self) -> frozenset[str]:
        """返回本位置非空的归一化名称/别名集合。"""
        return _candidate_normalized_labels(self._labels())

    def _labels(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class ProvenanceOutcome:
    """证据规则判定结果。

    - ``field_sources``:白名单关键字段 → 来源;
    - ``canonical_values``:可信来源下的规范化值(店铺别名 → ``str(store.id)``、
      金额 → 两位小数 ``Decimal``);不可信字段为 ``None``。
    """

    field_sources: Mapping[str, ArgumentTrustLevel]
    canonical_values: Mapping[str, Any]


class EvidenceResolver:
    """确定性证据规则引擎;不访问数据库,全部输入由调用方权威提供。"""

    def resolve(
        self,
        *,
        user_message: str,
        arguments: Mapping[str, Any],
        stores: Sequence[StoreCandidate] = (),
        locations: Sequence[LocationCandidate] = (),
        answered: Mapping[str, str] | None = None,
        sku_unique_in_workspace: bool | None = None,
        history: Sequence[str] = (),
    ) -> ProvenanceOutcome:
        """逐字段判定参数来源。

        ``history`` 是旧对话消息;证据来源白名单不包含旧对话,因此显式接收
        并有意忽略,避免调用方误以为历史消息参与判定。``sku_unique_in_workspace``
        是权威读取结果(SKU 在当前 Workspace 是否恰好唯一),``None`` 表示
        未做权威读取。

        P1 写目标 ``store_ref`` / ``location_ref`` 与 ``store_id`` 同属"可被
        模型猜测的业务目标",只接受 USER_EXPLICIT / USER_CONFIRMED;
        ``store_ref`` 的规范化值不被本引擎改写(Service 只接受名称/别名,
        不接受内部 UUID),``location_ref`` 缺省时不产生来源(由 Service 在
        唯一启用位置时确定性解析)。
        """
        del history
        normalized_message = normalize_label(user_message)
        answers = answered or {}
        field_sources: dict[str, ArgumentTrustLevel] = {}
        canonical_values: dict[str, Any] = {}

        store_source, store_value = self._resolve_store(
            field="store_id",
            normalized_message=normalized_message,
            arguments=arguments,
            stores=stores,
            answers=answers,
        )
        field_sources["store_id"] = store_source
        canonical_values["store_id"] = store_value

        store_ref_source, _ = self._resolve_store(
            field="store_ref",
            normalized_message=normalized_message,
            arguments=arguments,
            stores=stores,
            answers=answers,
        )
        field_sources["store_ref"] = store_ref_source

        sku_source, sku_value = self._resolve_sku(
            normalized_message=normalized_message,
            arguments=arguments,
            answers=answers,
            sku_unique_in_workspace=sku_unique_in_workspace,
        )
        field_sources["sku"] = sku_source
        canonical_values["sku"] = sku_value

        field_sources["location_ref"] = self._resolve_location(
            normalized_message=normalized_message,
            arguments=arguments,
            locations=locations,
            answers=answers,
        )

        price_source, price_value = self._resolve_target_price(
            arguments=arguments, answers=answers
        )
        field_sources["target_price"] = price_source
        canonical_values["target_price"] = price_value

        return ProvenanceOutcome(
            field_sources=field_sources, canonical_values=canonical_values
        )

    def _resolve_store(
        self,
        *,
        field: str,
        normalized_message: str,
        arguments: Mapping[str, Any],
        stores: Sequence[StoreCandidate],
        answers: Mapping[str, str],
    ) -> tuple[ArgumentTrustLevel, str | None]:
        """``store_id`` / ``store_ref`` 只接受 USER_EXPLICIT / USER_CONFIRMED。

        返回值第二个元素是命中店铺的规范标识(``str(store.id)``);调用方按
        字段语义决定是否采用(``store_ref`` 不改写模型引用,只取来源等级)。
        """
        value = _trimmed_str(arguments.get(field))
        answered_store = _trimmed_str(answers.get(field))
        if value is not None:
            evidence = self._explicit_store_evidence(
                normalized_message, value, stores
            )
            if evidence is not None and evidence.matches_value(value):
                return ArgumentTrustLevel.USER_EXPLICIT, evidence.store_id
            if answered_store is not None and value == answered_store:
                matched = _candidate_by_value(answered_store, stores)
                if matched is not None:
                    return ArgumentTrustLevel.USER_CONFIRMED, matched.store_id
            return ArgumentTrustLevel.UNTRUSTED, None
        if answered_store is not None:
            matched = _candidate_by_value(answered_store, stores)
            if matched is not None:
                return ArgumentTrustLevel.USER_CONFIRMED, matched.store_id
        return ArgumentTrustLevel.UNTRUSTED, None

    def _resolve_location(
        self,
        *,
        normalized_message: str,
        arguments: Mapping[str, Any],
        locations: Sequence[LocationCandidate],
        answers: Mapping[str, str],
    ) -> ArgumentTrustLevel:
        """``location_ref`` 只接受 USER_EXPLICIT / USER_CONFIRMED。

        缺省(既未提供也未回答)时不参与判定:唯一启用位置由 Service 确定性
        解析,多位置由 Service 要求补充;此处只负责"模型**自行提供**的位置
        引用必须有用户证据"。
        """
        value = _trimmed_str(arguments.get("location_ref"))
        answered_location = _trimmed_str(answers.get("location_ref"))
        if value is not None:
            if self._explicit_location_evidence(normalized_message, value, locations):
                return ArgumentTrustLevel.USER_EXPLICIT
            if (
                answered_location is not None
                and value == answered_location
                and _location_by_value(answered_location, locations) is not None
            ):
                return ArgumentTrustLevel.USER_CONFIRMED
            return ArgumentTrustLevel.UNTRUSTED
        if answered_location is not None and _location_by_value(
            answered_location, locations
        ) is not None:
            return ArgumentTrustLevel.USER_CONFIRMED
        return ArgumentTrustLevel.UNTRUSTED

    def _resolve_sku(
        self,
        *,
        normalized_message: str,
        arguments: Mapping[str, Any],
        answers: Mapping[str, str],
        sku_unique_in_workspace: bool | None,
    ) -> tuple[ArgumentTrustLevel, str | None]:
        """``sku``:消息精确证据 > 结构化回答 > Workspace 唯一匹配。"""
        value = _trimmed_str(arguments.get("sku"))
        answered_sku = _trimmed_str(answers.get("sku"))
        if value is not None:
            normalized_value = normalize_label(value)
            if normalized_value and normalized_value in normalized_message:
                return ArgumentTrustLevel.USER_EXPLICIT, value
            if answered_sku is not None and value == answered_sku:
                return ArgumentTrustLevel.USER_CONFIRMED, value
            if sku_unique_in_workspace is True:
                return ArgumentTrustLevel.SYSTEM_RESOLVED, value
            return ArgumentTrustLevel.UNTRUSTED, None
        if answered_sku is not None:
            return ArgumentTrustLevel.USER_CONFIRMED, answered_sku
        return ArgumentTrustLevel.UNTRUSTED, None

    def _resolve_target_price(
        self, *, arguments: Mapping[str, Any], answers: Mapping[str, str]
    ) -> tuple[ArgumentTrustLevel, Decimal | None]:
        """``target_price``:合法金额是确定性规范化结果;非法金额不可信。"""
        raw = arguments.get("target_price")
        if raw is not None and (not isinstance(raw, str) or raw.strip()):
            amount = normalize_amount(raw)
            if amount is not None:
                return ArgumentTrustLevel.DETERMINISTIC_DERIVED, amount
            return ArgumentTrustLevel.UNTRUSTED, None
        answered_price = _trimmed_str(answers.get("target_price"))
        if answered_price is not None:
            amount = normalize_amount(answered_price)
            if amount is not None:
                return ArgumentTrustLevel.USER_CONFIRMED, amount
        return ArgumentTrustLevel.UNTRUSTED, None

    @staticmethod
    def _explicit_store_evidence(
        normalized_message: str,
        value: str,
        stores: Sequence[StoreCandidate],
    ) -> StoreCandidate | None:
        """ToolCall 目标必须唯一，且消息中至少有一个无歧义标签指向它。

        消息可以同时明确提到多家店；只要当前 ``store_id`` 唯一指向其中
        一家，就构成该次 ToolCall 的用户显式证据。若同一个消息标签映射到
        多家店（例如共享别名“旗舰店”），仍按歧义处理。
        """
        value_matches = [store for store in stores if store.matches_value(value)]
        if len(value_matches) != 1:
            return None
        selected = value_matches[0]
        for label in selected.matched_labels(normalized_message):
            owners = sum(label in store.normalized_labels() for store in stores)
            if owners == 1:
                return selected
        return None

    @staticmethod
    def _explicit_location_evidence(
        normalized_message: str,
        value: str,
        locations: Sequence[LocationCandidate],
    ) -> bool:
        """``location_ref`` 的目标必须唯一,且消息中至少有一个无歧义标签指向它。

        与店铺证据同规则:消息可以同时点名多个仓,只要当前 ``location_ref``
        唯一指向其中之一即构成用户显式证据;共享别名导致的歧义不做证据。
        """
        value_matches = [loc for loc in locations if loc.matches_value(value)]
        if len(value_matches) != 1:
            return False
        selected = value_matches[0]
        for label in selected.matched_labels(normalized_message):
            owners = sum(label in loc.normalized_labels() for loc in locations)
            if owners == 1:
                return True
        return False


def _candidate_matches_value(
    primary_id: str, labels: tuple[str, ...], value: str
) -> bool:
    """候选标识是否被 ``value`` 指向(内部 id 精确匹配或归一化后名称/别名匹配)。"""
    normalized = normalize_label(value)
    if not normalized:
        return False
    if value.strip() == primary_id:
        return True
    return any(normalize_label(label) == normalized for label in labels)


def _candidate_matched_labels(
    labels: tuple[str, ...], normalized_message: str
) -> frozenset[str]:
    """归一化消息中实际命中的归一化名称/别名。"""
    return frozenset(
        label
        for label in (normalize_label(item) for item in labels)
        if label and label in normalized_message
    )


def _candidate_normalized_labels(labels: tuple[str, ...]) -> frozenset[str]:
    """候选非空的归一化名称/别名集合。"""
    return frozenset(
        label for label in (normalize_label(item) for item in labels) if label
    )


def _trimmed_str(value: Any) -> str | None:
    """非空白字符串 → trim 后的值;其余(缺失/非字符串)→ ``None``。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _candidate_by_value(
    value: str, stores: Sequence[StoreCandidate]
) -> StoreCandidate | None:
    """``value`` 唯一指向的店铺;零匹配或多匹配都返回 ``None``(不猜歧义)。"""
    matches = [store for store in stores if store.matches_value(value)]
    return matches[0] if len(matches) == 1 else None


def _location_by_value(
    value: str, locations: Sequence[LocationCandidate]
) -> LocationCandidate | None:
    """``value`` 唯一指向的位置;零匹配或多匹配都返回 ``None``(不猜歧义)。"""
    matches = [loc for loc in locations if loc.matches_value(value)]
    return matches[0] if len(matches) == 1 else None


__all__ = [
    "TRUSTED_TRUST_LEVELS",
    "ArgumentTrustLevel",
    "EvidenceResolver",
    "LocationCandidate",
    "ProvenanceOutcome",
    "StoreCandidate",
]
