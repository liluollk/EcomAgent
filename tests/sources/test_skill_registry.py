"""测试 SkillRegistry — SKILL.md 文件制注册表（解析/扫描/CRUD/热加载/解析路由）。"""

import pytest

from sources.skill_registry import (
    Skill,
    SkillRegistry,
    parse_skill_md,
    to_skill_md,
)


# ----------------------------------------------------------------------
# Skill 与 SKILL.md 解析
# ----------------------------------------------------------------------

def test_skill_creation():
    skill = Skill(
        name="inventory_query",
        description="查询商品库存",
        keywords=["库存", "存货"],
        body="执行库存查询 SOP：1) 确认渠道与 SKU。",
    )
    assert skill.name == "inventory_query"
    assert "库存" in skill.keywords
    assert "SOP" in skill.body


def test_parse_skill_md_frontmatter_and_body():
    text = (
        "---\n"
        "name: demo_skill\n"
        "description: 演示技能\n"
        "keywords: [库存, 调价]\n"
        "prerequisites: [source:taobao]\n"
        "---\n"
        "\n"
        "执行演示 SOP：\n"
        "1) 第一步；\n"
        "2) 第二步。\n"
    )
    skill = parse_skill_md(text)
    assert skill.name == "demo_skill"
    assert skill.description == "演示技能"
    assert skill.keywords == ["库存", "调价"]
    assert skill.prerequisites == ["source:taobao"]
    assert skill.body.startswith("执行演示 SOP")
    assert skill.enabled is True
    assert skill.builtin is False


def test_parse_skill_md_enabled_false():
    text = "---\nname: x\ndescription: d\nenabled: false\n---\n\n正文"
    assert parse_skill_md(text).enabled is False


def test_parse_skill_md_rejects_bad_format():
    with pytest.raises(ValueError):
        parse_skill_md("没有 frontmatter 的文本")
    with pytest.raises(ValueError):
        parse_skill_md("---\ndescription: 缺名字\n---\n正文")


def test_skill_md_roundtrip():
    skill = Skill(
        name="promo",
        description="促销 SOP",
        keywords=["促销", "折扣"],
        prerequisites=["source:douyin"],
        body="执行促销 SOP：1) 确认折扣。",
        enabled=False,
    )
    parsed = parse_skill_md(to_skill_md(skill))
    assert parsed.name == skill.name
    assert parsed.keywords == skill.keywords
    assert parsed.prerequisites == skill.prerequisites
    assert parsed.body == skill.body
    assert parsed.enabled is False


# ----------------------------------------------------------------------
# 目录扫描与 CRUD（tmp 隔离，不碰真实 data/）
# ----------------------------------------------------------------------

@pytest.fixture()
def dirs(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    (builtin / "inventory_query").mkdir(parents=True)
    (builtin / "inventory_query" / "SKILL.md").write_text(
        "---\nname: inventory_query\ndescription: 查询商品库存\nkeywords: [库存, 存货]\n---\n\n"
        "执行库存查询 SOP：1) 确认渠道与 SKU；2) 调用 query_inventory。",
        encoding="utf-8",
    )
    user.mkdir()
    return builtin, user


def test_builtin_scan_and_readonly(dirs):
    builtin, user = dirs
    reg = SkillRegistry(builtin_dir=builtin, user_dir=user)
    skill = reg.get("inventory_query")
    assert skill is not None and skill.builtin
    assert "query_inventory" in skill.body
    with pytest.raises(ValueError):
        reg.update("inventory_query", {"description": "篡改"})
    with pytest.raises(ValueError):
        reg.remove("inventory_query")
    with pytest.raises(ValueError):
        reg.set_enabled("inventory_query", False)


def test_user_skill_crud_persists_files(dirs):
    builtin, user = dirs
    reg = SkillRegistry(builtin_dir=builtin, user_dir=user)
    reg.add(Skill(name="my_skill", description="自定义", keywords=["自定义"], body="SOP 正文"))
    assert (user / "my_skill" / "SKILL.md").is_file()

    reg.update("my_skill", {"description": "改过"})
    assert reg.get("my_skill").description == "改过"

    reg.set_enabled("my_skill", False)
    assert "my_skill" not in reg.list_menu_names()

    reg.remove("my_skill")
    assert reg.get("my_skill") is None
    assert not (user / "my_skill").exists()


def test_add_rejects_duplicate_and_builtin_name(dirs):
    builtin, user = dirs
    reg = SkillRegistry(builtin_dir=builtin, user_dir=user)
    with pytest.raises(ValueError):
        reg.add(Skill(name="inventory_query", description="撞名内置"))
    with pytest.raises(ValueError):
        reg.add(Skill(name="x", description="d"))
        reg.add(Skill(name="x", description="d2"))


def test_mtime_hot_reload(dirs):
    builtin, user = dirs
    reg = SkillRegistry(builtin_dir=builtin, user_dir=user)
    assert reg.get("late_skill") is None
    (user / "late_skill").mkdir()
    (user / "late_skill" / "SKILL.md").write_text(
        "---\nname: late_skill\ndescription: 后到的技能\n---\n\nSOP",
        encoding="utf-8",
    )
    assert reg.get("late_skill") is not None  # 无需重建，mtime 变化自动重载


def test_broken_skill_file_skipped(dirs):
    builtin, user = dirs
    (user / "broken").mkdir()
    (user / "broken" / "SKILL.md").write_text("坏文件", encoding="utf-8")
    reg = SkillRegistry(builtin_dir=builtin, user_dir=user)
    assert reg.get("broken") is None
    assert reg.get("inventory_query") is not None  # 坏文件不影响其余加载


# ----------------------------------------------------------------------
# 内存接口与关键词解析
# ----------------------------------------------------------------------

def test_skill_registry_register_unregister():
    registry = SkillRegistry(load_persisted=False)
    skill = Skill(name="inventory_query", description="查询库存", keywords=["库存"])
    registry.register(skill)
    assert registry.get("inventory_query") is skill
    registry.unregister("inventory_query")
    assert registry.get("inventory_query") is None


def test_skill_registry_resolve():
    registry = SkillRegistry(load_persisted=False)
    registry.register(Skill(name="inventory_query", description="查询库存", keywords=["库存", "存货"]))
    registry.register(Skill(name="price_management", description="价格管理", keywords=["价格", "调价"]))
    matched = registry.resolve("查询淘宝库存")
    assert len(matched) == 1
    assert matched[0].name == "inventory_query"


def test_skill_registry_resolve_multiple():
    registry = SkillRegistry(load_persisted=False)
    registry.register(Skill(name="inventory_query", description="查询库存", keywords=["库存"]))
    registry.register(Skill(name="price_management", description="价格管理", keywords=["价格"]))
    matched = registry.resolve("查询库存和价格")
    assert len(matched) == 2


def test_skill_registry_resolve_no_match():
    registry = SkillRegistry(load_persisted=False)
    registry.register(Skill(name="inventory_query", description="查询库存", keywords=["库存"]))
    assert registry.resolve("今天天气怎么样") == []


def test_skill_registry_get_all():
    registry = SkillRegistry(load_persisted=False)
    registry.register(Skill(name="s1", description="d1", keywords=["k1"]))
    registry.register(Skill(name="s2", description="d2", keywords=["k2"]))
    all_skills = registry.get_all()
    assert len(all_skills) == 2
    assert "s1" in all_skills


def test_default_registry_has_builtin_skills():
    """默认注册表扫描真实 skills_builtin 目录：9 个内置电商技能。"""
    from sources.skill_registry import create_default_registry

    reg = create_default_registry()
    names = set(reg.list_menu_names())
    assert {"inventory_query", "price_management", "after_sales", "product_listing"} <= names
    assert all(s.builtin for s in reg.list())