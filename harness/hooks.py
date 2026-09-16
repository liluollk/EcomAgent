"""场景钩子 — 前置准备 / 收尾验证与场景表解耦。

钩子名在 harness/cases.py 的 scenario.setup / scenario.after 字段引用，
实现在本模块注册。pytest 入口与 harness 独立入口共用同一份注册表。

当前评测集（调价闭环 21 条）不引用任何钩子——它的执行契约由
harness/execution_contract.py 直接从操作审计与平台副作用日志断言，
不需要为每个场景写收尾函数。本模块保留为**通用扩展点**：需要「跑完整轮
对话之后再检查某个进程外状态」的场景（跨会话记忆、用户技能落盘、
动态渠道启用等）在此注册即可，场景表加一行字段就生效。
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
    from agent_core import memory_store as _ms

    store = _ms.DEFAULT_MEMORY_STORE
    assert "3 万件" in store.recall_section("default", query="双11")


def _after_skill_created() -> None:
    """技能创建场景收尾：断言 save_skill 已落盘并热加载进注册表菜单。"""
    from sources.skill_registry import DEFAULT_SKILL_REGISTRY

    skill = DEFAULT_SKILL_REGISTRY.get("stock_check_pro")
    assert skill is not None, "save_skill 后新技能应出现在注册表"
    # 沉淀的 SOP 必须指向默认调价闭环工具（快照入口），而不是未注册的扩展工具
    assert "query_product_snapshot" in skill.body
    assert "stock_check_pro" in DEFAULT_SKILL_REGISTRY.list_menu_names()


def _after_memory_forgotten() -> None:
    """记忆遗忘场景收尾：断言显式记忆已删除、不再被检索注入。"""
    from agent_core import memory_store as _ms

    store = _ms.DEFAULT_MEMORY_STORE
    assert "3 万件" not in store.recall_section("default", query="双11"), (
        "「忘记」后记忆不应再被检索注入"
    )


def _after_single_price_write() -> None:
    """调价写场景收尾：断言两个平台的调价副作用合计只落库一次。

    注意：常规场景不需要它——harness/execution_contract.py 用 step.expect 的
    writes_delta 按步断言。本钩子留给「整轮对话跨多步、只应发生一次写」的场景。
    """
    from mock_commerce.store import writes_of

    writes = [w for op in ("taobao_price_update", "douyin_price_update") for w in writes_of(op)]
    assert len(writes) == 1, (
        f"调价副作用应只落库一次（重试与恢复都走幂等），实际 {len(writes)} 次: {writes}"
    )


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