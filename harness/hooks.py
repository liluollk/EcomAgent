"""场景钩子 — 前置准备 / 收尾验证与场景表解耦。

钩子名在 harness/cases.py 的 scenario.setup / scenario.after 字段引用，
实现在本模块注册。pytest 入口与 harness 独立入口共用同一份注册表。
"""

from __future__ import annotations

from typing import Callable, Optional

from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY


def _setup_add_pdd_channel() -> None:
    """动态渠道场景前置：注册新渠道 pdd 并确认已生效。"""
    DEFAULT_CHANNEL_REGISTRY.add({"name": "pdd", "label": "拼多多"})
    assert "pdd" in DEFAULT_CHANNEL_REGISTRY.enabled_names()


def _after_memory_landed() -> None:
    """跨会话记忆场景收尾：断言显式记忆已落盘且可检索注入。"""
    from agent_runtime import memory_store as _ms

    store = _ms.DEFAULT_MEMORY_STORE
    assert "3 万件" in store.recall_section("default", query="双11")


def _after_skill_created() -> None:
    """技能创建场景收尾：断言 save_skill 已落盘并热加载进注册表菜单。"""
    from sources.skill_registry import DEFAULT_SKILL_REGISTRY

    skill = DEFAULT_SKILL_REGISTRY.get("stock_check_pro")
    assert skill is not None, "save_skill 后新技能应出现在注册表"
    assert "query_inventory" in skill.body
    assert "stock_check_pro" in DEFAULT_SKILL_REGISTRY.list_menu_names()


def _after_memory_forgotten() -> None:
    """记忆遗忘场景收尾：断言显式记忆已删除、不再被检索注入。"""
    from agent_runtime import memory_store as _ms

    store = _ms.DEFAULT_MEMORY_STORE
    assert "3 万件" not in store.recall_section("default", query="双11"), (
        "「忘记」后记忆不应再被检索注入"
    )


def _after_single_price_write() -> None:
    """幂等写场景收尾：断言重试链路里写副作用只真实落库一次。"""
    from mock_commerce.store import writes_of

    writes = writes_of("update_price")
    assert len(writes) == 1, (
        f"update_price 副作用应只落库一次（重试走幂等回放），实际 {len(writes)} 次: {writes}"
    )
    assert writes[0]["data"]["new_price"] == 89.0, f"落库价格应为 89.0: {writes[0]}"


SETUP_HOOKS: dict[str, Callable[[], None]] = {"add_pdd_channel": _setup_add_pdd_channel}
AFTER_HOOKS: dict[str, Callable[[], None]] = {
    "assert_memory_landed": _after_memory_landed,
    "assert_skill_created": _after_skill_created,
    "assert_memory_forgotten": _after_memory_forgotten,
    "assert_single_price_write": _after_single_price_write,
}


def get_setup_hook(name: Optional[str]) -> Optional[Callable[[], None]]:
    return SETUP_HOOKS.get(name or "")


def get_after_hook(name: Optional[str]) -> Optional[Callable[[], None]]:
    return AFTER_HOOKS.get(name or "")