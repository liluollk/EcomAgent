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