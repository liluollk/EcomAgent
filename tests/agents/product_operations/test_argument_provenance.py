"""参数归一化与 ArgumentProvenance 测试(计划 03 Task 3;架构文档 §5)。

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
from agents.product_operations.input.provenance import ArgumentProvenance
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
    """一个 Workspace 内的确定性种子:淘宝/豆音两店与两个商品。"""

    workspace: Workspace
    taobao: Any
    douyin: Any
    products: dict[str, Any]


async def _seed_input_catalog(session: AsyncSession) -> InputCatalog:
    from agents.product_operations.persistence.models import Product, Store

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
    return InputCatalog(
        workspace=workspace,
        taobao=taobao,
        douyin=douyin,
        products=products,
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
    assert result.provenance["store_id"] == ArgumentProvenance.UNTRUSTED


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
    assert result.provenance["store_id"] == ArgumentProvenance.USER_EXPLICIT
    assert result.provenance["sku"] == ArgumentProvenance.USER_EXPLICIT
    assert result.provenance["target_price"] == ArgumentProvenance.DETERMINISTIC_DERIVED
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
    assert result.provenance["store_id"] == ArgumentProvenance.USER_EXPLICIT
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
    assert result.provenance["store_id"] == ArgumentProvenance.USER_EXPLICIT
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
    assert result.provenance["store_id"] == ArgumentProvenance.UNTRUSTED
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
    assert result.provenance["sku"] == ArgumentProvenance.SYSTEM_RESOLVED
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
    assert result.provenance["sku"] == ArgumentProvenance.UNTRUSTED
    assert result.requirement is not None
    assert "sku" in result.requirement.field_paths


async def test_answered_request_store_is_user_confirmed(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    """已处理 Input Request 的结构化回答是合法证据;越权回答仍被拒绝。"""
    actor = actor_with_taobao_only(input_catalog)
    base = {
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
    assert confirmed.provenance["store_id"] == ArgumentProvenance.USER_CONFIRMED
    assert confirmed.requirement is None

    out_of_scope = await input_resolver.resolve(
        **base, answered={"store_id": str(input_catalog.douyin.id)}
    )
    assert out_of_scope.provenance["store_id"] == ArgumentProvenance.UNTRUSTED
    assert out_of_scope.requirement is not None
    assert "store_id" in out_of_scope.requirement.field_paths


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
    assert result.provenance["store_id"] == ArgumentProvenance.UNTRUSTED
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
    assert padded_result.provenance["store_id"] == ArgumentProvenance.UNTRUSTED
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
    assert result.provenance["store_id"] == ArgumentProvenance.UNTRUSTED
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
    assert result.provenance["target_price"] == ArgumentProvenance.UNTRUSTED


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


# ---- PreToolUse 注入协议适配(计划 02 Task 6) ----


async def test_pretool_adapters_conform_to_injection_protocol(
    input_resolver: ProductInputRequirementResolver,
    input_catalog: InputCatalog,
) -> None:
    actor = actor_with_taobao_only(input_catalog)

    normalized = await ProductPreToolNormalizer(
        input_resolver,
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
        diagnostic=SchemaDiagnostic(field_paths=("store_id",), message="缺少店铺")
    )
    assert requirement is not None
    assert requirement.field_paths == ("store_id",)
    store_options = requirement.options["store_id"]
    assert [option["value"] for option in store_options] == [str(input_catalog.taobao.id)]
