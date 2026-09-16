"""测试 builtin_tools 的 save_skill — skill-creator 元技能的落盘工具（校验/持久化/热加载/ACL）。"""

import asyncio

import pytest

import sources.skill_registry as sr
from sources import builtin_tools


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolate_skills(tmp_path, monkeypatch):
    """重定向注册表单例的用户技能目录，测试后还原。"""
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    reg = sr.DEFAULT_SKILL_REGISTRY
    old_dir, old_mtime = reg._user_dir, reg._cached_mtime
    reg._user_dir = tmp_path / "skills"
    reg._cached_mtime = -1
    yield tmp_path / "skills"
    reg._user_dir = old_dir
    reg._cached_mtime = -1


def test_save_skill_creates_file_and_hotloads(isolate_skills):
    r = _run(builtin_tools.save_skill(
        name="my_flow", description="我的流程", body="执行 SOP 正文", keywords=["测试"],
    ))
    assert "已创建" in r
    assert (isolate_skills / "my_flow" / "SKILL.md").is_file()
    skill = sr.DEFAULT_SKILL_REGISTRY.get("my_flow")
    assert skill is not None and skill.body == "执行 SOP 正文"
    assert "my_flow" in sr.DEFAULT_SKILL_REGISTRY.list_menu_names()


def test_save_skill_validates_name_description_body(isolate_skills):
    assert "不合法" in _run(builtin_tools.save_skill(name="Bad-Name", description="d", body="b"))
    assert "不合法" in _run(builtin_tools.save_skill(name="1abc", description="d", body="b"))
    assert "description 必填" in _run(builtin_tools.save_skill(name="ok_name", description="", body="b"))
    assert "SOP 正文必填" in _run(builtin_tools.save_skill(name="ok_name", description="d", body="   "))


def test_save_skill_rejects_duplicate(isolate_skills):
    _run(builtin_tools.save_skill(name="dup", description="d", body="b"))
    r = _run(builtin_tools.save_skill(name="dup", description="d", body="b2"))
    assert "已存在" in r


def test_save_skill_acl_roles():
    """save_skill 是写操作：店长/运营可创建技能，客服/财务不可。"""
    from permission.rbac import can_role_write

    assert can_role_write("manager", "save_skill")
    assert can_role_write("operator", "save_skill")
    assert not can_role_write("customer_service", "save_skill")
    assert not can_role_write("finance", "save_skill")


def test_skill_creator_builtin_in_menu():
    """skill_creator 内置技能在默认注册表菜单中（渐进式披露的元技能）。"""
    reg = sr.DEFAULT_SKILL_REGISTRY
    reg._maybe_reload()
    skill = reg.get("skill_creator")
    assert skill is not None and skill.builtin
    assert "save_skill" in skill.body


def test_query_inventory_does_not_expose_cost_price():
    result = _run(builtin_tools.query_inventory("taobao", "SKU-001"))

    assert "库存 1523 件" in result
    assert "成本价" not in result


# ----------------------------------------------------------------------
# Task 2：默认工具收敛为调价闭环 + 扩展注册表
# ----------------------------------------------------------------------

def test_default_tool_set_is_price_loop_only():
    """默认 get_definitions / get_handlers / tool_names 只暴露三个调价闭环工具。"""
    assert set(builtin_tools.tool_names()) == {
        "query_product_snapshot", "update_price", "save_skill",
    }
    assert {d["name"] for d in builtin_tools.get_definitions()} == set(builtin_tools.tool_names())
    assert set(builtin_tools.get_handlers()) == set(builtin_tools.tool_names())


def test_extension_registry_excludes_default_and_has_extension_tools():
    """扩展注册表含其余 10 个能力；关键扩展工具不在默认表，默认工具也不在扩展表。"""
    ext_defs = {d["name"] for d in builtin_tools.get_extension_definitions()}
    ext_handlers = set(builtin_tools.get_extension_handlers())
    ext = set(builtin_tools.EXTENSION_TOOL_NAMES)
    assert ext_defs == ext_handlers == ext
    assert len(ext) == 10
    # 必须新增的断言：这些能力不在默认定义/处理器中，但存在于扩展注册表
    for name in ("service_ticket", "query_knowledge_base", "query_anomalies", "query_after_sales_stats"):
        assert name not in builtin_tools.tool_names(), f"{name} 不应出现在默认工具"
        assert name in ext, f"{name} 应在扩展注册表"
    # 默认工具不应泄漏进扩展表
    for name in ("query_product_snapshot", "update_price", "save_skill"):
        assert name not in ext


class _FakePricePlatform:
    """内存假调价执行面，供离线确定性测试 update_price → 协调器接线。"""

    platform = "taobao"

    def __init__(self, verify_consistent: bool = True) -> None:
        self.verify_consistent = verify_consistent

    async def query_snapshot(self, ref):
        from decimal import Decimal

        from integrations.commerce.price_models import ProductSnapshot

        return ProductSnapshot(product_ref=ref, name="x", current_price=Decimal("100"),
                               stock=10, status="on")

    async def apply_price(self, command, *, idempotency_key=None):
        from integrations.commerce.price_models import PriceWriteReceipt

        return PriceWriteReceipt(product_ref=command.product_ref, applied_price=command.target_price)

    async def verify_price(self, ref, expected_price):
        from integrations.commerce.price_models import PriceVerification

        return PriceVerification(product_ref=ref, expected_price=expected_price,
                                observed_price=expected_price, consistent=self.verify_consistent)


def test_update_price_success_routes_to_coordinator_and_records_operation_id(tmp_path, monkeypatch):
    """成功路径：文本含「已更新」「回查一致」；协调器用同一 operation_id 落盘 approval_decided。"""
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(builtin_tools, "get_price_platform", lambda p: _FakePricePlatform())
    result = _run(builtin_tools.update_price(
        platform="taobao", product_id="ITEM-1001", sku_id="SKU-001",
        target_price=89.0, operation_id="op-fixed-1",
    ))
    assert "已更新" in result and "回查一致" in result
    store = builtin_tools.OperationStore(str(tmp_path))
    records = store.load("op-fixed-1")
    assert any(r.get("operation_id") == "op-fixed-1" for r in records)
    assert any(r.get("type") == "approval_decided" for r in records)


def test_update_price_blocked_by_cost_protection(tmp_path, monkeypatch):
    """拦截路径：低于成本价被成本保护拦截，文本含「拦截」+ 原因。"""
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path))
    # 成本价来自 MockCostProvider（mock_commerce.store）：taobao/SKU-001 成本 59
    monkeypatch.setattr(builtin_tools, "get_price_platform", lambda p: _FakePricePlatform())
    result = _run(builtin_tools.update_price(
        platform="taobao", product_id="ITEM-1001", sku_id="SKU-001",
        target_price=10.0, operation_id="op-cost-1",
    ))
    assert "拦截" in result
    assert "成本" in result


def test_update_price_rejected_when_verify_inconsistent(tmp_path, monkeypatch):
    """回查不一致：文本含「未生效」（REJECTED），不抛异常。"""
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(builtin_tools, "get_price_platform",
                        lambda p: _FakePricePlatform(verify_consistent=False))
    result = _run(builtin_tools.update_price(
        platform="taobao", product_id="ITEM-1001", sku_id="SKU-001",
        target_price=89.0, operation_id="op-rej-1",
    ))
    assert "未生效" in result
