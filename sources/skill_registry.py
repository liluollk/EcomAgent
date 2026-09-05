"""
技能注册表 — SKILL.md 文件制（对齐业界 Agent Skills 格式）。

Skill = 纯运营知识（SOP 操作手册 / 平台规则），不绑定工具：
- 格式：frontmatter（name/description/keywords/prerequisites/enabled）
  + markdown SOP 正文；
- 来源：内置 sources/skills_builtin/<name>/SKILL.md（只读、恒启用）
  + 用户 data/skills/<name>/SKILL.md（前端/agent 可创建、可启停）；
- 渐进式披露：菜单（name+description）常驻系统提示词，正文在
  load_skill 命中后注入；工具面与技能无关（全部工具常可见）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class Skill:
    """技能定义 — 运营知识包：SOP 操作手册 / 平台规则。

    Attributes:
        name: 技能名称，如 "inventory_query"。
        description: 技能描述，用于 LLM 意图识别（菜单常驻展示）。
        keywords: 触发关键词列表，如 ["库存", "存货", "查库存"]。
        prerequisites: 前置条件列表，如 ["source:taobao"]。
        body: SOP 正文（markdown）——加载时注入系统提示词，指导 LLM 按步骤执行。
        enabled: 是否启用（停用的技能不进菜单、不参与解析；内置技能恒启用）。
        builtin: 是否为内置技能（内置只读，不可编辑/删除/停用）。
        created_at: 创建时间戳（用户技能，内存记录）。
    """

    name: str
    description: str
    keywords: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    body: str = ""
    enabled: bool = True
    builtin: bool = False
    created_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# SKILL.md 解析 / 序列化（frontmatter 为 YAML 子集：key: value / key: [a, b]）
# ----------------------------------------------------------------------

def _parse_list_value(v: str) -> list[str]:
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    if not v:
        return []
    return [x.strip() for x in v.split(",") if x.strip()]


def parse_skill_md(text: str, *, builtin: bool = False) -> Skill:
    """SKILL.md 文本 → Skill。frontmatter 缺 name 或格式错误时抛 ValueError。"""
    text = text.lstrip("\ufeff").strip()
    if not text.startswith("---"):
        raise ValueError("SKILL.md 必须以 --- frontmatter 开头")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter 未闭合")
    data: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip()] = v.strip()
    name = data.get("name", "")
    if not name:
        raise ValueError("SKILL.md frontmatter 缺少 name")
    return Skill(
        name=name,
        description=data.get("description", ""),
        keywords=_parse_list_value(data.get("keywords", "")),
        prerequisites=_parse_list_value(data.get("prerequisites", "")),
        body=parts[2].strip(),
        enabled=data.get("enabled", "true").lower() != "false",
        builtin=builtin,
    )


def to_skill_md(skill: Skill) -> str:
    """Skill → SKILL.md 文本（用户技能持久化格式）。"""
    lines = ["---", f"name: {skill.name}", f"description: {skill.description}"]
    if skill.keywords:
        lines.append(f"keywords: [{', '.join(skill.keywords)}]")
    if skill.prerequisites:
        lines.append(f"prerequisites: [{', '.join(skill.prerequisites)}]")
    if not skill.enabled:
        lines.append("enabled: false")
    lines += ["---", "", skill.body, ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 目录约定
# ----------------------------------------------------------------------

def _builtin_dir() -> Path:
    return Path(__file__).resolve().parent / "skills_builtin"


def _user_dir() -> Path:
    env = os.environ.get("SKILLS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "skills"


def _scan_dir(root: Path, builtin: bool) -> list[Skill]:
    """扫描 <root>/<name>/SKILL.md，坏文件跳过（不影响其余技能加载）。"""
    out: list[Skill] = []
    if not root.exists():
        return out
    for sub in sorted(root.iterdir()):
        f = sub / "SKILL.md"
        if not f.is_file():
            continue
        try:
            out.append(parse_skill_md(f.read_text(encoding="utf-8"), builtin=builtin))
        except (ValueError, OSError):
            continue
    return out


class SkillRegistry:
    """技能注册表：SKILL.md 文件制 + mtime 热加载 + 用户技能 CRUD。

    内置技能（sources/skills_builtin/）只读恒启用；用户技能
    （data/skills/，env SKILLS_DIR 可覆盖）可新增/编辑/启停/删除。
    """

    def __init__(
        self,
        load_persisted: bool = True,
        builtin_dir: Optional[Path] = None,
        user_dir: Optional[Path] = None,
    ) -> None:
        """初始化技能注册表。

        Args:
            load_persisted: 是否扫描磁盘技能（测试注入内存技能时传 False）。
            builtin_dir: 覆盖内置技能目录（测试用）。
            user_dir: 覆盖用户技能目录（测试用）。
        """
        self._load_persisted = load_persisted
        self._builtin_dir = Path(builtin_dir) if builtin_dir else _builtin_dir()
        self._user_dir = Path(user_dir) if user_dir else _user_dir()
        self._skills: dict[str, Skill] = {}
        self._cached_mtime = -1.0
        if load_persisted:
            self._load_all()

    # ------------------------------------------------------------------
    # 加载与热更新
    # ------------------------------------------------------------------

    def _tree_mtime(self) -> float:
        m = 0.0
        for root in (self._builtin_dir, self._user_dir):
            if not root.exists():
                continue
            m = max(m, root.stat().st_mtime)
            for sub in root.iterdir():
                f = sub / "SKILL.md"
                if f.is_file():
                    m = max(m, f.stat().st_mtime)
        return m

    def _maybe_reload(self) -> None:
        if not self._load_persisted:
            return
        cur = self._tree_mtime()
        if cur != self._cached_mtime:
            self._load_all()
            self._cached_mtime = cur

    def _load_all(self) -> None:
        """扫描内置 + 用户目录（内置优先，同名用户技能被跳过）。"""
        self._skills = {}
        for skill in _scan_dir(self._builtin_dir, builtin=True):
            skill.enabled = True  # 内置技能恒启用
            self._skills[skill.name] = skill
        for skill in _scan_dir(self._user_dir, builtin=False):
            if skill.name in self._skills:
                continue
            skill.created_at = skill.created_at or int(time.time())
            self._skills[skill.name] = skill

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list(self) -> list[Skill]:
        """返回全部技能（含 disabled）。"""
        self._maybe_reload()
        return list(self._skills.values())

    def list_menu(self) -> list[tuple[str, str]]:
        """技能菜单：(name, description) 列表，仅启用技能（供 load_skill 注入）。"""
        self._maybe_reload()
        return [(s.name, s.description) for s in self._skills.values() if s.enabled]

    def list_menu_names(self) -> list[str]:
        """已启用技能名列表（供状态消息/错误提示）。"""
        self._maybe_reload()
        return [s.name for s in self._skills.values() if s.enabled]

    def get(self, skill_name: str) -> Optional[Skill]:
        """获取指定名称的技能（含 disabled，供前置条件/提示词读取）。"""
        self._maybe_reload()
        return self._skills.get(skill_name)

    def get_all(self) -> dict[str, Skill]:
        """获取所有已注册的技能。"""
        self._maybe_reload()
        return dict(self._skills)

    def resolve(self, user_message: str) -> list[Skill]:
        """根据用户消息解析匹配的技能（关键词子串匹配，跳过 disabled）。

        Args:
            user_message: 用户输入的消息文本。

        Returns:
            list[Skill]: 匹配到的已启用技能列表。
        """
        self._maybe_reload()
        matched = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            for keyword in skill.keywords:
                if keyword in user_message:
                    matched.append(skill)
                    break  # 一个技能只匹配一次
        return matched

    # ------------------------------------------------------------------
    # 用户技能 CRUD（内置只读）
    # ------------------------------------------------------------------

    def _is_builtin(self, name: str) -> bool:
        skill = self._skills.get(name)
        return bool(skill and skill.builtin)

    def add(self, skill: Skill) -> Skill:
        """新增用户技能（写 data/skills/<name>/SKILL.md）。"""
        self._maybe_reload()
        if skill.name in self._skills:
            raise ValueError(f"技能已存在: {skill.name}")
        if skill.builtin or self._is_builtin(skill.name):
            raise ValueError(f"内置技能 {skill.name} 不可重复新增")
        skill.created_at = skill.created_at or int(time.time())
        target = self._user_dir / skill.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(to_skill_md(skill), encoding="utf-8")
        self._skills[skill.name] = skill
        self._cached_mtime = self._tree_mtime()
        return skill

    def update(self, name: str, patch: dict[str, Any]) -> Skill:
        """更新用户技能（改 keywords/prerequisites/body/enabled 等）。内置技能只读。"""
        self._maybe_reload()
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(name)
        if skill.builtin:
            raise ValueError(f"内置技能 {name} 只读，不可编辑")
        updated = Skill(**{
            **skill.to_dict(),
            **{k: v for k, v in patch.items() if k not in {"name", "builtin", "created_at"}},
        })
        updated.builtin = False
        updated.created_at = skill.created_at
        (self._user_dir / name / "SKILL.md").write_text(to_skill_md(updated), encoding="utf-8")
        self._skills[name] = updated
        self._cached_mtime = self._tree_mtime()
        return updated

    def remove(self, name: str) -> None:
        """删除用户技能（内置技能不可删）。"""
        self._maybe_reload()
        if self._is_builtin(name):
            raise ValueError(f"内置技能 {name} 不可删除")
        if name not in self._skills:
            raise KeyError(name)
        import shutil

        shutil.rmtree(self._user_dir / name, ignore_errors=True)
        del self._skills[name]
        self._cached_mtime = self._tree_mtime()

    def set_enabled(self, name: str, enabled: bool) -> Skill:
        """启停用户技能（内置技能恒启用）。"""
        if self._is_builtin(name):
            raise ValueError(f"内置技能 {name} 恒启用，不可停用")
        return self.update(name, {"enabled": enabled})

    # ------------------------------------------------------------------
    # 内存接口（测试注入 / 无持久化场景）
    # ------------------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """注册一个技能到注册表（仅内存；不写盘）。"""
        self._skills[skill.name] = skill

    def unregister(self, skill_name: str) -> None:
        """注销一个技能（内存）。"""
        self._skills.pop(skill_name, None)


def create_default_registry() -> SkillRegistry:
    """创建含内置技能的注册表（扫描 skills_builtin，不读用户目录）。"""
    registry = SkillRegistry(load_persisted=False)
    for skill in _scan_dir(_builtin_dir(), builtin=True):
        skill.enabled = True
        registry.register(skill)
    return registry


# 模块级默认注册表：TurnLifecycle 未显式注入时使用（文件制 + mtime 热加载）
DEFAULT_SKILL_REGISTRY = SkillRegistry()