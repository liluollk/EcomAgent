"""测试 SkillRegistry — 技能注册和解析。"""

import pytest
from sources.skill_registry import Skill, SkillRegistry


def test_skill_creation():
    skill = Skill(
        name="inventory_query",
        description="查询商品库存",
        keywords=["库存", "存货"],
    )
    assert skill.name == "inventory_query"
    assert "库存" in skill.keywords


def test_skill_registry_register():
    registry = SkillRegistry()
    skill = Skill(name="inventory_query", description="查询库存", keywords=["库存"])
    registry.register(skill)
    assert registry.get("inventory_query") is skill


def test_skill_registry_unregister():
    registry = SkillRegistry()
    skill = Skill(name="inventory_query", description="查询库存", keywords=["库存"])
    registry.register(skill)
    registry.unregister("inventory_query")
    assert registry.get("inventory_query") is None


def test_skill_registry_resolve():
    registry = SkillRegistry()
    registry.register(Skill(
        name="inventory_query",
        description="查询库存",
        keywords=["库存", "存货"],
    ))
    registry.register(Skill(
        name="price_management",
        description="价格管理",
        keywords=["价格", "调价"],
    ))
    matched = registry.resolve("查询淘宝库存")
    assert len(matched) == 1
    assert matched[0].name == "inventory_query"


def test_skill_registry_resolve_multiple():
    registry = SkillRegistry()
    registry.register(Skill(
        name="inventory_query",
        description="查询库存",
        keywords=["库存"],
    ))
    registry.register(Skill(
        name="price_management",
        description="价格管理",
        keywords=["价格"],
    ))
    matched = registry.resolve("查询库存和价格")
    assert len(matched) == 2


def test_skill_registry_resolve_no_match():
    registry = SkillRegistry()
    registry.register(Skill(
        name="inventory_query",
        description="查询库存",
        keywords=["库存"],
    ))
    matched = registry.resolve("今天天气怎么样")
    assert len(matched) == 0


def test_skill_registry_get_all():
    registry = SkillRegistry(load_persisted=False)  # 不加载磁盘持久化技能
    registry.register(Skill(name="s1", description="d1", keywords=["k1"]))
    registry.register(Skill(name="s2", description="d2", keywords=["k2"]))
    all_skills = registry.get_all()
    assert len(all_skills) == 2
    assert "s1" in all_skills