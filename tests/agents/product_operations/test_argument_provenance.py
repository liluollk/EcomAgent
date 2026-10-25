"""参数归一化与 ArgumentTrustLevel 测试(计划 03 Task 3;架构文档 §5)。

覆盖:
- 计划原文用例:模型编造店铺必须转 Input;店铺候选项限制在授权范围;
- 证据来源白名单:当前用户文本精确证据(USER_EXPLICIT)、已处理 Input
  Request 的结构化回答(USER_CONFIRMED)、Workspace 权威数据唯一匹配
  (SYSTEM_RESOLVED)、确定性金额规范化(DETERMINISTIC_DERIVED),
  其余一律 UNTRUSTED;
- store_id 只接受 USER_EXPLICIT/USER_CONFIRMED;sku 允许唯一
  SYSTEM_RESOLVED;target_price 允许 DETERMINISTIC_DERIVED;
- 旧对话内容、模型自报 source/confidence、"只有一家授权店"均不是证据;
- 金额格式非法 → 校验诊断,而不是 Input Request;
- 归一化:字段 trim、金额字符串 → 两位小数 Decimal、字段白名单;
- 与 runtime PreToolUse 注入协议(计划 02 Task 6)的适配。

按 Task 2 的约定,店铺标识使用 ``str(store.id)``(UUID 字符串);计划原文
``"store-taobao"`` 字面量以 UUID 字符串等价替换,语义(选项被限制到授权
店铺)不变。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.input.normalizer import (
    normalize_amount,
    normalize_arguments,
)
from agents.product_operations.input.profiles import p1_fields_for
from agents.product_operations.input.provenance import ArgumentTrustLevel
from agents.product_operations.input.requirements import (
    ProductInputRequirementResolver,
    ProductPreToolInputRequirementResolver,
    ProductPreToolNormalizer,
    ProductPreToolProvenanceResolver,
)
from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import (
    ArgumentProvenance as RuntimeArgumentProvenance,
)
from runtime.execution.pretool.decisions import SchemaDiagnostic
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


# ---- 种子与 Actor 构造 ----


@dataclass(frozen=True)
class InputCatalog:
    """一个 Workspace 内的确定性种子:淘宝/豆音两店、两个商品与一个履约位置。"""

    workspace: Workspace
    taobao: Any
    douyin: Any
    products: dict[str, Any]
    location: Any


async def _seed_input_catalog(session: AsyncSession) -> InputCatalog:
    from agents.product_operations.persistence.models import (
        FulfillmentLocation,
        Product,
        Store,
    )

    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    taobao = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"taobao-{uuid.uuid4()}",
        name="淘宝旗舰店",
        aliases=["淘宝旗舰店"],
    )
    douyin = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.DOUYIN,
        external_store_ref=f"douyin-{uuid.uuid4()}",
        name="豆音小店",
        aliases=["豆音小店"],
    )
    products = {
        "SKU-001": Product(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            sku="SKU-001",
            title="轻量保温杯 500ml",
        ),
        "SKU-002": Product(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            sku="SKU-002",
            title="陶瓷马克杯",
        ),
    }
    session.add(workspace)
    await session.flush()
    session.add_all((taobao, douyin, *products.values()))
    await session.flush()
    location = FulfillmentLocation(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=taobao.id,
        external_location_ref=f"loc-{uuid.uuid4()}",
        name="默认仓",
        aliases=["默认仓"],
    )
    session.add(location)
    await session.flush()
    return InputCatalog(
        workspace=workspace,
        taobao=taobao,
        douyin=douyin,
        products=products,
        location=location,
    )


def _actor(
    *, workspace_id: uuid.UUID, store_ids: frozenset[str]
) -> ActorContext:
    """构造测试用 ActorContext;store scope 的 resource_id = str(store.id)。"""
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=frozenset({"product.read"}),
        resource_scopes={
            "store": ResourceScope(mode="SELECTED", resource_ids=store_ids),
        },
        request_id="req-argument-provenance",
    )


def actor_with_taobao_only(catalog: InputCatalog) -> ActorContext:
    """仅授权淘宝店的运营者(计划原文用例的 actor 构造)。"""
    return _actor(
        workspace_id=catalog.workspace.id,
        store_ids=frozenset({str(catalog.taobao.id)}),
    )


def actor_with_all_stores(catalog: InputCatalog) -> ActorContext:
    """授权淘宝店与豆音店的运营者。"""
    return _actor(
        workspace_id=catalog.workspace.id,
        store_ids=frozenset({str(catalog.taobao.id), str(catalog.douyin.id)}),
    )


def actor_with_douyin_only(catalog: InputCatalog) -> ActorContext:
    """只授权豆音店的运营者(用于"目标店铺越权"的对照)。"""
    return _actor(
        workspace_id=catalog.workspace.id,
        store_ids=frozenset({str(catalog.douyin.id)}),
    )


@pytest.fixture()
async def input_catalog(db_session: AsyncSession) -> InputCatalog:
    return await _seed_input_catalog(db_session)


@pytest.fixture()
def input_resolver(db_session: AsyncSession) -> ProductInputRequirementResolver:
    return ProductInputRequirementResolver(session=db_session)


# ---- 计划原文用例 ----


async def test_model_supplied_store_without_evidence_requires_input(
    input_resolver: ProductInputRequirementResolver,
) -> None:
    result = await input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元",
        arguments={"store_id": "store-taobao", "sku": "SKU-001", "target_price": "99.00"},
    )
    assert result.requirement is not None
    assert result.requirement.field_paths == ("store_id",)
    assert result.provenance["store_id"] == ArgumentTrustLevel.UNTRUSTED


async def test_store_options_are_limited_to_actor_scope(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    result = await input_resolver.store_requirement(actor_with_taobao_only(input_catalog))
    assert [option.value for option in result.options] == [str(input_catalog.taobao.id)]


# ---- 证据来源白名单 ----


async def test_store_alias_in_current_message_is_user_explicit(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 改成 99 元",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
    )
    assert result.provenance["store_id"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.provenance["sku"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.provenance["target_price"] == ArgumentTrustLevel.DETERMINISTIC_DERIVED
    assert result.requirement is None
    assert result.diagnostics == ()


async def test_store_alias_argument_is_canonicalized_via_alias_table(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """USER_EXPLICIT:消息证据经别名表确定性归一化为 str(store.id)。"""
    result = await input_resolver.resolve(
        user_message="把 淘宝旗舰店 的 SKU-001 改成 99 元",
        arguments={"store_id": " 淘宝旗舰店 ", "sku": "SKU-001", "target_price": "99.00"},
        actor=actor_with_taobao_only(input_catalog),
    )
    assert result.provenance["store_id"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.normalized_arguments["store_id"] == str(input_catalog.taobao.id)
    assert result.requirement is None


@pytest.mark.parametrize("target_store", ["taobao", "douyin"])
async def test_each_explicit_store_in_multi_store_message_is_trusted(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    target_store: str,
) -> None:
    """多店比较中，每个被用户独立点名的店铺都可作为当前 ToolCall 证据。"""
    selected = getattr(input_catalog, target_store)
    result = await input_resolver.resolve(
        user_message="对比淘宝旗舰店和豆音小店的 SKU-001 价格",
        arguments={
            "store_id": str(selected.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_all_stores(input_catalog),
    )
    assert result.provenance["store_id"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.normalized_arguments["store_id"] == str(selected.id)
    assert result.requirement is None


async def test_ambiguous_alias_evidence_is_not_trusted(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    db_session: AsyncSession,
) -> None:
    """别名同时命中多家授权店 → 歧义,不构成证据。"""
    from agents.product_operations.persistence.models import Store

    douyin = await db_session.get(Store, input_catalog.douyin.id)
    assert douyin is not None
    douyin.aliases = ["旗舰店"]
    input_catalog.taobao.aliases = ["旗舰店"]
    await db_session.flush()

    result = await input_resolver.resolve(
        user_message="在旗舰店把 SKU-001 改成 99 元",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_all_stores(input_catalog),
    )
    assert result.provenance["store_id"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert "store_id" in result.requirement.field_paths


async def test_sku_unique_workspace_match_is_system_resolved(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """SKU 不在消息中,但在当前 Workspace 唯一匹配 → SYSTEM_RESOLVED。"""
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的价格改成 99 元",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
    )
    assert result.provenance["sku"] == ArgumentTrustLevel.SYSTEM_RESOLVED
    assert result.requirement is None


async def test_sku_without_workspace_match_is_untrusted(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的价格改成 99 元",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-404",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
    )
    assert result.provenance["sku"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert "sku" in result.requirement.field_paths


async def test_answered_request_store_is_user_confirmed(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """已处理 Input Request 的结构化回答是合法证据;越权回答仍被拒绝。"""
    actor = actor_with_taobao_only(input_catalog)
    base: dict[str, Any] = {
        "user_message": "把 SKU-001 改成 99 元",
        "arguments": {
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        "actor": actor,
    }
    confirmed = await input_resolver.resolve(
        **base, answered={"store_id": str(input_catalog.taobao.id)}
    )
    assert confirmed.provenance["store_id"] == ArgumentTrustLevel.USER_CONFIRMED
    assert confirmed.requirement is None

    out_of_scope = await input_resolver.resolve(
        **base, answered={"store_id": str(input_catalog.douyin.id)}
    )
    assert out_of_scope.provenance["store_id"] == ArgumentTrustLevel.UNTRUSTED
    assert out_of_scope.requirement is not None
    assert "store_id" in out_of_scope.requirement.field_paths


# ---- P1 写目标的来源守卫:store_ref / location_ref ----


async def test_model_supplied_store_ref_without_evidence_requires_input(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """P1 写 Tool 用 store_ref;模型自行补的店铺引用必须有用户证据。"""
    result = await input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
        fields=p1_fields_for("set_listing_price"),
    )
    assert result.provenance["store_ref"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert "store_ref" in result.requirement.field_paths
    # store_ref 的规范值不被改写:Service 只接受名称/别名,不接受内部 UUID。
    assert result.normalized_arguments["store_ref"] == "淘宝旗舰店"


async def test_store_ref_named_in_message_is_user_explicit(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 改成 99 元",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
        fields=p1_fields_for("set_listing_price"),
    )
    assert result.provenance["store_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.requirement is None


async def test_answered_store_ref_is_user_confirmed(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """已处理的 Input Request 回答(store_ref 名称)可作证据;越权回答仍被拒。"""
    base: dict[str, Any] = {
        "user_message": "把 SKU-001 改成 99 元",
        "arguments": {
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        "actor": actor_with_taobao_only(input_catalog),
        "fields": p1_fields_for("set_listing_price"),
    }
    confirmed = await input_resolver.resolve(
        **base, answered={"store_ref": "淘宝旗舰店"}
    )
    assert confirmed.provenance["store_ref"] == ArgumentTrustLevel.USER_CONFIRMED
    assert confirmed.requirement is None

    # 回答指向本 Workspace 不存在的店铺 → 不构成证据。
    bogus = await input_resolver.resolve(**base, answered={"store_ref": "查无此店"})
    assert bogus.provenance["store_ref"] == ArgumentTrustLevel.UNTRUSTED
    assert bogus.requirement is not None


async def test_provided_location_ref_without_evidence_requires_input(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """多仓场景:模型自行补一个合法 location_ref 不等于用户选择,必须回到 Input。"""
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "location_ref": "默认仓",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_taobao_only(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert result.provenance["store_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.provenance["location_ref"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert result.requirement.field_paths == ("location_ref",)


async def test_location_ref_named_in_message_is_user_explicit(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店默认仓的 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "location_ref": "默认仓",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_taobao_only(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert result.provenance["location_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.requirement is None


async def test_absent_location_ref_is_not_required(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """缺省 location_ref 不进守卫:唯一启用位置仍由 Service 确定性解析。"""
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_taobao_only(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert "location_ref" not in result.normalized_arguments
    assert result.requirement is None


async def _add_location(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    store_id: uuid.UUID,
    name: str,
) -> None:
    """为指定店铺追加一个启用履约位置(证据作用域测试用)。"""
    from agents.product_operations.persistence.models import FulfillmentLocation

    session.add(
        FulfillmentLocation(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            store_id=store_id,
            external_location_ref=f"loc-{uuid.uuid4()}",
            name=name,
            aliases=[name],
        )
    )
    await session.flush()


async def test_location_evidence_is_scoped_to_resolved_store(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    db_session: AsyncSession,
) -> None:
    """两个店铺都有同名"默认仓"时,已明确的"店铺 + 仓库"仍是 USER_EXPLICIT。

    位置证据必须先按 ``store_ref`` 解析出的规范 ``store_id`` 收窄候选;否则跨店铺
    同名仓会被判成歧义,把用户已经点明的目标错误降级为 UNTRUSTED。
    """
    await _add_location(
        db_session,
        workspace_id=input_catalog.workspace.id,
        store_id=input_catalog.douyin.id,
        name="默认仓",
    )

    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店默认仓的 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "location_ref": "默认仓",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_all_stores(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert result.provenance["store_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.provenance["location_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.requirement is None


async def test_location_evidence_without_store_scope_stays_ambiguous(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    db_session: AsyncSession,
) -> None:
    """店铺证据缺失时不收窄作用域:跨店铺同名仓仍按歧义处理(对照组)。"""
    await _add_location(
        db_session,
        workspace_id=input_catalog.workspace.id,
        store_id=input_catalog.douyin.id,
        name="默认仓",
    )

    result = await input_resolver.resolve(
        user_message="把 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "location_ref": "默认仓",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_all_stores(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert result.provenance["store_ref"] == ArgumentTrustLevel.UNTRUSTED
    assert result.provenance["location_ref"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert "location_ref" in result.requirement.field_paths


async def test_multiple_locations_without_location_ref_requires_choice(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    db_session: AsyncSession,
) -> None:
    """目标店铺已确定、有多个启用位置、又未给出 location_ref → 要求补充。

    这正是"多位置要求用户补充":PreToolUse 阶段就产生 InputRequest,而不是进入
    Handler 后才得到确定性失败。候选项限定在目标店铺内。
    """
    await _add_location(
        db_session,
        workspace_id=input_catalog.workspace.id,
        store_id=input_catalog.taobao.id,
        name="备用仓",
    )

    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_all_stores(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert result.provenance["store_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.requirement is not None
    assert result.requirement.field_paths == ("location_ref",)
    options = result.requirement.options["location_ref"]
    assert {option["value"] for option in options} == {"默认仓", "备用仓"}


async def test_out_of_scope_store_does_not_require_location_choice(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    db_session: AsyncSession,
) -> None:
    """越权店铺即使多位置也不生成位置需求:交由 Access Control 确定性拒绝。

    位置证据取自 Workspace 全量别名表,越权店铺的用户显式证据仍会产生
    ``USER_EXPLICIT``;但"多位置必须补充输入"属于业务前提,只应在 Actor 有权
    操作的店铺上提出,否则等于向用户追问一个无权操作的目标。
    """
    await _add_location(
        db_session,
        workspace_id=input_catalog.workspace.id,
        store_id=input_catalog.taobao.id,
        name="备用仓",
    )

    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 加 5 件",
        arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "delta": 5,
            "reason": "RECEIPT",
        },
        actor=actor_with_douyin_only(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )

    assert result.provenance["store_ref"] == ArgumentTrustLevel.USER_EXPLICIT
    assert result.requirement is None


async def test_out_of_scope_store_leaks_no_location_candidates(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    db_session: AsyncSession,
) -> None:
    """location_ref 已提供但不可信 + 目标店铺越权 → 不返回该店铺的位置候选。

    候选项在 Access Control **之前**生成,因此位置候选必须以授权店铺为界;
    否则越权店铺的仓库名称会经由 Input Request 泄露。
    """
    await _add_location(
        db_session,
        workspace_id=input_catalog.workspace.id,
        store_id=input_catalog.taobao.id,
        name="备用仓",
    )
    arguments = {
        "store_ref": "淘宝旗舰店",
        "sku": "SKU-001",
        "location_ref": "默认仓",
        "delta": 5,
        "reason": "RECEIPT",
    }
    # 消息点明店铺但不含位置证据,因此 location_ref 只能来自模型自补。
    user_message = "把淘宝旗舰店的 SKU-001 加 5 件"

    scoped = await input_resolver.resolve(
        user_message=user_message,
        arguments=arguments,
        actor=actor_with_douyin_only(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert scoped.provenance["location_ref"] == ArgumentTrustLevel.UNTRUSTED
    assert scoped.requirement is not None
    assert scoped.requirement.field_paths == ("location_ref",)
    assert "location_ref" not in scoped.requirement.options

    # 对照面:同一请求在授权店铺的 Actor 下正常给出店铺内位置候选。
    authorized = await input_resolver.resolve(
        user_message=user_message,
        arguments=arguments,
        actor=actor_with_all_stores(input_catalog),
        fields=p1_fields_for("adjust_inventory_quantity"),
    )
    assert authorized.requirement is not None
    assert {
        option["value"] for option in authorized.requirement.options["location_ref"]
    } == {"默认仓", "备用仓"}


# ---- 明确不受信的证据来源 ----


async def test_history_messages_are_not_evidence(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """旧对话内容即使提到店铺,也不作为当前证据。"""
    result = await input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
        history=("上一轮:请把淘宝旗舰店的 SKU-001 改成 99 元。",),
    )
    assert result.provenance["store_id"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert "store_id" in result.requirement.field_paths


async def test_model_reported_source_and_confidence_are_ignored(
    input_resolver: ProductInputRequirementResolver,
) -> None:
    base = {"store_id": "store-taobao", "sku": "SKU-001", "target_price": "99.00"}
    padded = {
        **base,
        "source": "user_message",
        "confidence": 0.99,
        "store_id_source": "user",
    }
    plain = await input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元", arguments=base
    )
    padded_result = await input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元", arguments=padded
    )
    assert plain.provenance == padded_result.provenance
    assert padded_result.provenance["store_id"] == ArgumentTrustLevel.UNTRUSTED
    # 白名单之外的字段被归一化直接丢弃。
    assert set(padded_result.normalized_arguments) == set(base)


async def test_single_authorized_store_is_not_evidence(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """只有一家授权店不构成证据:合法 UUID 也必须来自用户文本或确认。"""
    result = await input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        },
        actor=actor_with_taobao_only(input_catalog),
    )
    assert result.provenance["store_id"] == ArgumentTrustLevel.UNTRUSTED
    assert result.requirement is not None
    assert result.requirement.field_paths == ("store_id",)


# ---- 金额校验与归一化 ----


@pytest.mark.parametrize("bad_price", ["abc", "99.999", "-1.00"])
async def test_invalid_price_fails_validation_instead_of_input(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
    bad_price: str,
) -> None:
    result = await input_resolver.resolve(
        user_message="把淘宝旗舰店的 SKU-001 改价",
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": bad_price,
        },
        actor=actor_with_taobao_only(input_catalog),
    )
    assert result.requirement is None
    assert result.diagnostics[0].field_paths == ("target_price",)
    assert result.provenance["target_price"] == ArgumentTrustLevel.UNTRUSTED


def test_normalize_arguments_trims_fields_and_converts_amounts() -> None:
    normalized = normalize_arguments(
        {
            "store_id": " s-1 ",
            "sku": " SKU-001 ",
            "target_price": " 99 ",
            "source": "user",
            "confidence": 0.99,
        }
    )
    assert normalized == {
        "store_id": "s-1",
        "sku": "SKU-001",
        "target_price": Decimal("99.00"),
    }
    assert normalize_amount("99.5") == Decimal("99.50")
    assert normalize_amount(99) == Decimal("99.00")
    assert normalize_amount("99.999") is None
    assert normalize_amount("abc") is None


def test_normalize_amount_accepts_json_numbers() -> None:
    """模型写 JSON 数字是接口常见形态:按 str 转十进制,不因浮点误差拒绝。"""
    assert normalize_amount(52.35) == Decimal("52.35")
    assert normalize_amount(88.0) == Decimal("88.00")
    assert normalize_amount(0.1) == Decimal("0.10")
    # 仍不静默舍入:三位小数照旧拒绝
    assert normalize_amount(52.355) is None
    assert normalize_amount(float("nan")) is None
    assert normalize_amount(float("inf")) is None


# ---- PreToolUse 注入协议适配(计划 02 Task 6) ----


async def test_pretool_adapters_conform_to_injection_protocol(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    actor = actor_with_taobao_only(input_catalog)

    normalized = await ProductPreToolNormalizer(
        input_resolver,
        fields=("store_id", "sku", "target_price"),
        user_message="把淘宝旗舰店的 SKU-001 改成 99 元",
        actor=actor,
    ).normalize(
        arguments={
            "store_id": "淘宝旗舰店",
            "sku": " SKU-001 ",
            "target_price": "99",
        }
    )
    assert normalized["store_id"] == str(input_catalog.taobao.id)
    assert normalized["target_price"] == Decimal("99.00")

    provenance_resolver = ProductPreToolProvenanceResolver(
        input_resolver,
        user_message="把淘宝旗舰店的 SKU-001 改成 99 元",
        actor=actor,
    )
    provenance = await provenance_resolver.resolve(
        arguments={
            "store_id": str(input_catalog.taobao.id),
            "sku": "SKU-001",
            "target_price": "99.00",
        }
    )
    assert isinstance(provenance, RuntimeArgumentProvenance)
    assert provenance.field_sources["store_id"] == "USER_EXPLICIT"

    requirement_resolver = ProductPreToolInputRequirementResolver(
        input_resolver, actor=actor
    )
    requirement = await requirement_resolver.resolve(
        diagnostic=SchemaDiagnostic(field_paths=("store_id",), message="缺少店铺"),
        arguments={},
    )
    assert requirement is not None
    assert requirement.field_paths == ("store_id",)
    store_options = requirement.options["store_id"]
    assert [option["value"] for option in store_options] == [str(input_catalog.taobao.id)]
