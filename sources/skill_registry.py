"""
技能注册表 — 管理 Agent 技能的定义、生命周期与持久化。

SkillRegistry。
技能是 Agent 能力的抽象单位，每个技能关联一组工具、触发关键词与 SOP 指令段。

渐进式技能加载（Progressive Skill Loading）：
- 意图路由：关键词匹配（轻量）判断用户请求属于哪个技能域；
- 按域加载：命中技能时只在其工具集上叠加基础只读工具（见 base_agent）；
- SOP 注入：命中时把技能 prompt（SOP 指令段）注入系统提示词，指导执行步骤；
- 用户可在前端新增/启停/编辑技能，配置持久化到 data/skills.json。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# 内置技能名（不可删除，但可启停）
BUILTIN_NAMES = {
    "inventory_query",
    "price_management",
    "promotion_management",
    "order_management",
    "order_analytics",
    "anomaly_detection",
    "knowledge_inquiry",
    "after_sales",
    "product_listing",
}


@dataclass
class Skill:
    """技能定义，包含名称、描述、关键词、前置条件、关联工具与 SOP 指令。

    Attributes:
        name: 技能名称，如 "inventory_query"。
        description: 技能描述，用于 LLM 意图识别。
        keywords: 触发关键词列表，如 ["库存", "存货", "查库存"]。
        prerequisites: 前置条件列表，如 ["source:taobao"]。
        tools: 关联的工具名称列表。
        prompt: SOP 指令段——命中时注入系统提示词，指导 LLM 按步骤执行。
        enabled: 是否启用（停用的技能不参与解析）。
        builtin: 是否为内置技能（内置不可删除，但可启停）。
    """

    name: str
    description: str
    keywords: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    prompt: str = ""
    enabled: bool = True
    builtin: bool = False
    created_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _config_path() -> str:
    return os.environ.get("SKILL_CONFIG_FILE") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "skills.json")
    )


def _to_skill(data: dict[str, Any]) -> Skill:
    """dict → Skill（字段带默认值，兼容旧记录缺字段）。"""
    known = {f: data.get(f) for f in (
        "name", "description", "keywords", "prerequisites", "tools",
        "prompt", "enabled", "builtin", "created_at",
    )}
    known["name"] = str(known["name"])
    known["description"] = str(known.get("description") or "")
    for lst_key in ("keywords", "prerequisites", "tools"):
        val = known.get(lst_key)
        known[lst_key] = [str(x) for x in val] if isinstance(val, list) else []
    known["prompt"] = str(known.get("prompt") or "")
    known["enabled"] = bool(known.get("enabled", True))
    known["builtin"] = bool(known.get("builtin", False))
    return Skill(**known)


def _default_skills() -> list[Skill]:
    """内置 9 个电商运营技能（keywords/tools 与历史一致；prompt 为 SOP 指令段）。"""
    return [
        Skill(
            name="inventory_query",
            description="查询商品库存与补货建议",
            keywords=["库存", "存货", "补货", "盘点"],
            tools=["query_inventory"],
            prompt=(
                "执行库存查询 SOP：1) 先确认用户指定了渠道与 SKU；2) 调用 "
                "query_inventory 获取当前库存；3) 向用户汇报库存数量与商品名。"
            ),
            builtin=True,
        ),
        Skill(
            name="price_management",
            description="商品价格调整管理",
            keywords=["价格", "调价", "改价", "定价", "降价"],
            tools=["update_price"],
            prompt=(
                "执行调价 SOP：1) 确认目标价与成本价；2) 若新价格低于成本价则"
                "向用户说明无法执行（成本保护规则）；3) 调用 update_price 调整价格；"
                "4) 汇报新旧价格。"
            ),
            builtin=True,
        ),
        Skill(
            name="promotion_management",
            description="促销活动创建与活动检查",
            keywords=["促销", "优惠", "折扣", "满减", "活动检查"],
            tools=["create_promotion", "query_promotions", "query_inventory"],
            prompt=(
                "执行促销 SOP：1) 确认渠道/SKU/折扣/起止时间；2) 如需检查现有活动先"
                "调用 query_promotions；3) 调用 create_promotion 创建；4) 汇报活动信息。"
            ),
            builtin=True,
        ),
        Skill(
            name="order_management",
            description="订单状态查询与跟进",
            keywords=["订单", "发货", "物流"],
            tools=["query_order_status"],
            prompt=(
                "执行订单查询 SOP：1) 确认渠道与订单号；2) 调用 query_order_status "
                "查询；3) 汇报订单当前状态。"
            ),
            builtin=True,
        ),
        Skill(
            name="order_analytics",
            description="订单/销售分析",
            keywords=["销售分析", "成交", "销售额", "GMV", "订单分析"],
            tools=["query_order_stats"],
            prompt=(
                "执行销售分析 SOP：1) 确认渠道与统计周期；2) 调用 query_order_stats "
                "获取订单量/GMV/客单价；3) 汇总关键指标并给出结论。"
            ),
            builtin=True,
        ),
        Skill(
            name="anomaly_detection",
            description="经营异常排查（价格/库存/评分）",
            keywords=["异常", "预警", "排查", "风控"],
            tools=["query_anomalies"],
            prompt=(
                "执行异常排查 SOP：1) 确认渠道；2) 调用 query_anomalies 拉取异常项；"
                "3) 逐条向用户说明异常并给出处理建议。"
            ),
            builtin=True,
        ),
        Skill(
            name="knowledge_inquiry",
            description="经营知识库查询",
            keywords=["知识库", "规范", "话术", "政策"],
            tools=["query_knowledge_base"],
            prompt=(
                "执行知识库查询 SOP：1) 提炼用户主题；2) 调用 query_knowledge_base "
                "检索；3) 命中则引用条目回答，未命中则建议补充知识库。"
            ),
            builtin=True,
        ),
        Skill(
            name="after_sales",
            description="售后与客诉处理（工单/退款分析）",
            keywords=["售后", "客诉", "投诉", "退款", "工单"],
            tools=["query_order_status", "service_ticket", "query_after_sales_stats"],
            prompt=(
                "执行售后处理 SOP：1) 先查订单状态（query_order_status）；2) 根据"
                "客诉内容创建售后工单（service_ticket，注明优先级）；3) 汇报工单号。"
            ),
            builtin=True,
        ),
        Skill(
            name="product_listing",
            description="商品上架/下架管理",
            keywords=["上架", "下架", "新品发布", "下柜"],
            tools=["product_shelf", "query_inventory"],
            prompt=(
                "执行上下架 SOP：1) 确认渠道/SKU/动作（on=上架 off=下架）；2) 可选"
                "先查库存；3) 调用 product_shelf 执行；4) 汇报结果。"
            ),
            builtin=True,
        ),
    ]


class _Store:
    """JSON 配置读写 + mtime 缓存（技能持久化，跨进程 mtime 检测变更）。"""

    def __init__(self) -> None:
        self._mtime: float = -1
        self._skills: Optional[list[dict[str, Any]]] = None

    def load(self) -> list[dict[str, Any]]:
        path = _config_path()
        try:
            mtime = os.path.getmtime(path)
            if mtime != self._mtime:
                with open(path, "r", encoding="utf-8") as f:
                    import json

                    data = json.load(f)
                self._skills = data.get("skills", [])
                self._mtime = mtime
        except (OSError, ValueError):
            if self._skills is None:
                self._skills = [s.to_dict() for s in _default_skills()]
        return list(self._skills) if self._skills is not None else [s.to_dict() for s in _default_skills()]

    def save(self, skills: list[dict[str, Any]]) -> None:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            import json

            json.dump({"skills": skills}, f, ensure_ascii=False, indent=2)
        self._mtime = os.path.getmtime(path)
        self._skills = [dict(c) for c in skills]


_store = _Store()


class SkillRegistry:
    """技能注册表：关键词解析 + 持久化 CRUD（前端可新增/启停/编辑技能）。"""

    def __init__(self, load_persisted: bool = True) -> None:
        """初始化技能注册表。

        Args:
            load_persisted: 是否从磁盘加载持久化技能（测试注入自定义注册表时传 False）。
        """
        self._skills: dict[str, Skill] = {}
        if load_persisted:
            self._load_all()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """从磁盘加载全部技能（文件缺失时回退内置 9 个）。"""
        self._skills = {}
        for data in _store.load():
            skill = _to_skill(data)
            self._skills[skill.name] = skill

    def _save_all(self) -> None:
        """全量写盘（含内置技能，保持「内置+自定义」单文件结构）。"""
        _store.save([s.to_dict() for s in self._skills.values()])

    def list(self) -> list[Skill]:
        """返回全部技能（含 disabled）。"""
        return list(self._skills.values())

    def list_menu(self) -> list[tuple[str, str]]:
        """技能菜单：(name, description) 列表，仅启用技能（供 load_skill 注入）。"""
        return [
            (s.name, s.description)
            for s in self._skills.values()
            if s.enabled
        ]

    def list_menu_names(self) -> list[str]:
        """已启用技能名列表（供状态消息/错误提示）。"""
        return [s.name for s in self._skills.values() if s.enabled]

    def add(self, skill: Skill) -> Skill:
        """新增技能（用户自定义）。"""
        if skill.name in self._skills:
            raise ValueError(f"技能已存在: {skill.name}")
        if skill.builtin or skill.name in BUILTIN_NAMES:
            raise ValueError(f"内置技能 {skill.name} 不可重复新增")
        skill.created_at = skill.created_at or int(time.time())
        self._skills[skill.name] = skill
        self._save_all()
        return skill

    def update(self, name: str, patch: dict[str, Any]) -> Skill:
        """更新技能（改 keywords/tools/prompt/enabled 等）。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(name)
        updated = Skill(**{
            **skill.to_dict(),
            **{k: v for k, v in patch.items() if k not in {"name", "builtin", "created_at"}},
        })
        self._skills[name] = updated
        self._save_all()
        return updated

    def remove(self, name: str) -> None:
        """删除技能（内置技能不可删，仅可启停）。"""
        if name in BUILTIN_NAMES or (self._skills.get(name) and self._skills[name].builtin):
            raise ValueError(f"内置技能 {name} 不可删除，可停用")
        if name not in self._skills:
            raise KeyError(name)
        del self._skills[name]
        self._save_all()

    def set_enabled(self, name: str, enabled: bool) -> Skill:
        """启停技能。"""
        return self.update(name, {"enabled": enabled})

    # ------------------------------------------------------------------
    # 兼容旧接口
    # ------------------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """注册一个技能到注册表（内存；不写盘，供内置与测试注入）。"""
        self._skills[skill.name] = skill

    def unregister(self, skill_name: str) -> None:
        """注销一个技能（内存）。"""
        self._skills.pop(skill_name, None)

    def get(self, skill_name: str) -> Optional[Skill]:
        """获取指定名称的技能（含 disabled，供前置条件/提示词读取）。"""
        return self._skills.get(skill_name)

    def resolve(self, user_message: str) -> list[Skill]:
        """根据用户消息解析匹配的技能（关键词子串匹配，跳过 disabled）。

        Args:
            user_message: 用户输入的消息文本。

        Returns:
            list[Skill]: 匹配到的已启用技能列表。
        """
        matched = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            for keyword in skill.keywords:
                if keyword in user_message:
                    matched.append(skill)
                    break  # 一个技能只匹配一次
        return matched

    def get_all(self) -> dict[str, Skill]:
        """获取所有已注册的技能。"""
        return dict(self._skills)


def create_default_registry() -> SkillRegistry:
    """创建含内置技能的注册表（用于测试注入 / 无持久化场景）。"""
    registry = SkillRegistry(load_persisted=False)
    for skill in _default_skills():
        registry.register(skill)
    return registry


# 模块级默认注册表：TurnLifecycle 未显式注入时使用（持久化驱动）
DEFAULT_SKILL_REGISTRY = SkillRegistry()
