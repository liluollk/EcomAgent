"""多仓位置需求的作用域闸门(设计 §4.1、§5;复评 P1)。

位置证据的匹配集是 Workspace 全量别名表,可能指向 Actor 越权店铺;而候选项在
Access Control **之前**生成。若不按授权收窄,"多位置要求补充"与位置候选都会
越过授权边界。

本模块用**不依赖数据库**的纯逻辑用例固定这两条契约,使它们在 PostgreSQL 未启动
时(此时聚焦 pytest 只会得到 ``ConnectionRefusedError``)依然有可执行的断言:

1. 越权店铺不产生"多位置要求补充";
2. ``requirement_for`` 不为越权店铺产出 ``location_ref`` 候选项;
3. 适配层把"无 SchemaDiagnostic 的业务需求"接通,且不涉及位置的 Tool 不触发
   额外的权威读取;
4. ``require_input`` 在没有诊断时用 ``InputRequirement.field_paths`` 作为
   ``detail.field_paths``。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from agents.product_operations.input.provenance import (
    LocationCandidate,
    ProvenanceOutcome,
)
from agents.product_operations.input.requirements import (
    ProductInputRequirementResolver,
    ProductPreToolInputRequirementResolver,
)
from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import require_input
from runtime.interfaces.identifiers import ResourceScope
from runtime.interfaces.requests import InputRequirement
from runtime.persistence.base import MembershipRole


WORKSPACE_ID = uuid.uuid4()
TAOBAO_ID = str(uuid.uuid4())
DOUYIN_ID = str(uuid.uuid4())

INVENTORY_FIELDS: tuple[str, ...] = (
    "store_ref",
    "sku",
    "location_ref",
    "delta",
    "reason",
)
PRICE_FIELDS: tuple[str, ...] = ("store_ref", "sku", "target_price")


def _actor(store_ids: frozenset[str] | None) -> ActorContext:
    """``store_ids=None`` → ``ALL``;否则为 ``SELECTED`` 集合。"""
    scope = (
        ResourceScope(mode="ALL")
        if store_ids is None
        else ResourceScope(mode="SELECTED", resource_ids=store_ids)
    )
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=frozenset({"product.read", "product.inventory.write"}),
        resource_scopes={"store": scope},
        request_id="req-location-scope",
    )


def _locations(*store_ids: str) -> tuple[LocationCandidate, ...]:
    return tuple(
        LocationCandidate(
            location_id=str(uuid.uuid4()),
            store_id=store_id,
            name=f"仓-{index}",
            aliases=(f"仓-{index}",),
        )
        for index, store_id in enumerate(store_ids)
    )


def _outcome(*, store_scope: str | None) -> ProvenanceOutcome:
    return ProvenanceOutcome(
        field_sources={}, canonical_values={}, store_scope=store_scope
    )


class _StubResolution:
    def __init__(self, requirement: InputRequirement | None) -> None:
        self.requirement = requirement
        self.store_scope: str | None = None


class _StubDomainResolver:
    """领域 resolver 的替身:记录调用次数与收到的参数。"""

    def __init__(self, resolution: _StubResolution) -> None:
        self._resolution = resolution
        self.calls = 0
        self.last_arguments: Mapping[str, Any] | None = None

    async def resolve(self, **arguments: Any) -> _StubResolution:
        self.calls += 1
        self.last_arguments = arguments.get("arguments")
        return self._resolution


# ---- 闸门 1:越权店铺不产生"多位置要求补充" ----


def test_requires_location_choice_is_gated_by_store_scope() -> None:
    outcome = _outcome(store_scope=TAOBAO_ID)
    multiple = _locations(TAOBAO_ID, TAOBAO_ID)
    provided = {"store_ref": "淘宝旗舰店", "sku": "SKU-1"}

    # 授权全部店铺 + 目标店铺多位置 → 要求补充。
    assert ProductInputRequirementResolver._requires_location_choice(
        INVENTORY_FIELDS, provided, outcome, multiple, actor=_actor(None)
    )
    # 目标店铺不在授权范围 → 不追问,交由 Access Control 拒绝。
    assert not ProductInputRequirementResolver._requires_location_choice(
        INVENTORY_FIELDS,
        provided,
        outcome,
        multiple,
        actor=_actor(frozenset({DOUYIN_ID})),
    )


def test_requires_location_choice_ignores_other_conditions() -> None:
    outcome = _outcome(store_scope=TAOBAO_ID)
    actor = _actor(None)
    # location_ref 已提供 → 缺参场景不成立(已提供的可信性由来源守卫处理)。
    assert not ProductInputRequirementResolver._requires_location_choice(
        INVENTORY_FIELDS,
        {"store_ref": "淘宝旗舰店", "location_ref": "默认仓"},
        outcome,
        _locations(TAOBAO_ID, TAOBAO_ID),
        actor=actor,
    )
    # 店铺内唯一启用位置 → 由 Service 确定性解析,不必追问。
    assert not ProductInputRequirementResolver._requires_location_choice(
        INVENTORY_FIELDS,
        {"store_ref": "淘宝旗舰店"},
        outcome,
        _locations(TAOBAO_ID),
        actor=actor,
    )
    # 工具没有声明 location_ref → 与位置无关。
    assert not ProductInputRequirementResolver._requires_location_choice(
        PRICE_FIELDS,
        {"store_ref": "淘宝旗舰店"},
        outcome,
        _locations(TAOBAO_ID, TAOBAO_ID),
        actor=actor,
    )
    # 店铺本身不可信(store_scope 为 None)→ 不按位置追问。
    assert not ProductInputRequirementResolver._requires_location_choice(
        INVENTORY_FIELDS,
        {"store_ref": "淘宝旗舰店"},
        _outcome(store_scope=None),
        _locations(TAOBAO_ID, TAOBAO_ID),
        actor=actor,
    )


# ---- 闸门 2:越权店铺的位置候选不外泄 ----


async def test_requirement_for_omits_out_of_scope_location_options() -> None:
    """越权店铺:``requirement_for`` 不产出位置候选(该分支不触库)。"""
    resolver = ProductInputRequirementResolver(session=None)  # type: ignore[arg-type]

    requirement = await resolver.requirement_for(
        ("location_ref",),
        actor=_actor(frozenset({DOUYIN_ID})),
        store_scope=TAOBAO_ID,
    )

    assert requirement.field_paths == ("location_ref",)
    assert "location_ref" not in requirement.options


async def test_requirement_for_without_store_scope_omits_location_options() -> None:
    """``store_scope`` 缺失时同样不生成位置候选(无法证明授权作用域)。"""
    resolver = ProductInputRequirementResolver(session=None)  # type: ignore[arg-type]

    requirement = await resolver.requirement_for(
        ("location_ref",), actor=_actor(None), store_scope=None
    )

    assert "location_ref" not in requirement.options


# ---- 闸门 3:业务输入需求接通适配层 ----


async def test_adapter_returns_business_requirement_without_diagnostic() -> None:
    requirement = InputRequirement(field_paths=("location_ref",), question="请选择位置")
    domain = _StubDomainResolver(_StubResolution(requirement))
    adapter = ProductPreToolInputRequirementResolver(
        domain, fields=INVENTORY_FIELDS, actor=_actor(None)  # type: ignore[arg-type]
    )

    resolved = await adapter.resolve(
        diagnostic=None, arguments={"store_ref": "淘宝旗舰店", "sku": "SKU-1"}
    )

    assert resolved is requirement
    assert domain.calls == 1


async def test_adapter_skips_domain_when_location_is_not_involved() -> None:
    domain = _StubDomainResolver(_StubResolution(None))
    adapter = ProductPreToolInputRequirementResolver(
        domain, fields=PRICE_FIELDS, actor=_actor(None)  # type: ignore[arg-type]
    )

    assert await adapter.resolve(diagnostic=None, arguments={"store_ref": "淘宝旗舰店"}) is None
    assert domain.calls == 0


async def test_adapter_skips_domain_when_location_ref_provided() -> None:
    domain = _StubDomainResolver(_StubResolution(None))
    adapter = ProductPreToolInputRequirementResolver(
        domain, fields=INVENTORY_FIELDS, actor=_actor(None)  # type: ignore[arg-type]
    )

    assert (
        await adapter.resolve(
            diagnostic=None,
            arguments={"store_ref": "淘宝旗舰店", "location_ref": "默认仓"},
        )
        is None
    )
    assert domain.calls == 0


async def test_adapter_without_actor_returns_no_business_requirement() -> None:
    domain = _StubDomainResolver(_StubResolution(InputRequirement(field_paths=("sku",))))
    adapter = ProductPreToolInputRequirementResolver(
        domain, fields=INVENTORY_FIELDS, actor=None  # type: ignore[arg-type]
    )

    assert await adapter.resolve(diagnostic=None, arguments={}) is None
    assert domain.calls == 0


# ---- 闸门 4:无诊断的 REQUIRE_INPUT 决策 ----


def test_require_input_without_diagnostic_uses_requirement_field_paths() -> None:
    decision = require_input(
        input_requirement=InputRequirement(
            field_paths=("location_ref",), question="请选择位置"
        )
    )

    assert decision.kind == "REQUIRE_INPUT"
    assert decision.schema_diagnostic is None
    assert decision.detail["field_paths"] == ("location_ref",)


def test_require_input_with_diagnostic_keeps_diagnostic_field_paths() -> None:
    """对照面:有诊断时 ``detail.field_paths`` 仍取诊断(既有语义不变)。"""
    from runtime.execution.pretool.decisions import SchemaDiagnostic

    decision = require_input(
        input_requirement=InputRequirement(
            field_paths=("location_ref",), question="请选择位置"
        ),
        schema_diagnostic=SchemaDiagnostic(
            field_paths=("store_ref", "location_ref"), message="untrusted"
        ),
    )

    assert decision.schema_diagnostic is not None
    assert decision.detail["field_paths"] == ("store_ref", "location_ref")
