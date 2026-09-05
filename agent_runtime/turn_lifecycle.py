"""
Turn 生命周期 — 管理单个 turn 的五个阶段：技能解析→前置条件→提示词→委托→完成。

TurnLifecycle 类。
实现 Template Method 模式中的五个步骤，每个步骤由 BaseAgent.chat() 按序调用。
"""

from __future__ import annotations

from typing import Any, Optional

from session.session import Session
from session.workspace import Workspace
from agent_backend.protocol import AgentBackend
from sources.skill_registry import Skill, SkillRegistry, DEFAULT_SKILL_REGISTRY


class TurnLifecycle:
    """管理单个 turn 的完整生命周期。

    五个阶段：
    1. resolve_skills: 通过 SkillRegistry 从用户消息中解析技能
    2. check_prerequisites: 检查技能前置条件（渠道激活状态等）
    3. build_system_prompt: 构建系统提示词
    4. [委托后端执行] — 由 BaseAgent.chat() 处理
    5. [完成] — 由 BaseAgent.chat() 发出 CompleteEvent

    用法:
        lifecycle = TurnLifecycle(session, workspace, backend)
        skills = lifecycle.resolve_skills("查询库存")
        if lifecycle.check_prerequisites(skills):
            prompt = lifecycle.build_system_prompt()
    """

    def __init__(
        self,
        session: Session,
        workspace: Workspace,
        backend: Any,
        registry: Optional[SkillRegistry] = None,
    ) -> None:
        """初始化 Turn 生命周期管理器。

        Args:
            session: 当前会话对象。
            workspace: 当前工作空间对象。
            backend: 后端实例（用于获取能力信息）。
            registry: 技能注册表，缺省使用内置电商技能注册表。
        """
        self._session = session
        self._workspace = workspace
        self._backend = backend
        self._registry = registry or DEFAULT_SKILL_REGISTRY

    def resolve_skills(self, user_message: str) -> list[str]:
        """从用户消息中解析技能（实例方法，使用实例配置的注册表）。

        委托 SkillRegistry 进行关键词匹配，如"库存"→"inventory_query"；
        无匹配时回退到 general_query。

        Args:
            user_message: 用户输入的消息文本。

        Returns:
            list[str]: 匹配到的技能名称列表。
        """
        return TurnLifecycle.resolve_skills_static(user_message, self._registry)

    @staticmethod
    def resolve_skills_static(user_message: str, registry: SkillRegistry) -> list[str]:
        """静态版本：从用户消息中解析技能，使用指定注册表。

        供 BaseAgent._resolve_skills_and_tools 独立调用。

        Args:
            user_message: 用户输入的消息文本。
            registry: 技能注册表。

        Returns:
            list[str]: 匹配到的技能名称列表。
        """
        matched = registry.resolve(user_message)
        return [skill.name for skill in matched] if matched else ["general_query"]

    def unmet_prerequisites(self, resolved_skills: list[str]) -> list[str]:
        """计算技能未满足的前置条件。

        规则：前置条件 "source:<name>" 要求 <name> 在 session.active_sources 中。

        Args:
            resolved_skills: 解析出的技能名称列表。

        Returns:
            list[str]: 未满足前置条件的描述列表，为空表示全部满足。
        """
        unmet: list[str] = []
        for name in resolved_skills:
            skill = self._registry.get(name)
            if skill is None:
                continue
            for prereq in skill.prerequisites:
                if prereq.startswith("source:"):
                    source_name = prereq[len("source:"):]
                    if source_name not in self._session.active_sources:
                        unmet.append(f"技能 {name} 需要激活渠道 {source_name}")
                else:
                    unmet.append(f"技能 {name} 存在无法识别的前置条件 {prereq}")
        return unmet

    def check_prerequisites(self, resolved_skills: list[str]) -> bool:
        """检查前置条件是否满足。

        Args:
            resolved_skills: 解析出的技能列表。

        Returns:
            bool: 前置条件是否满足。
        """
        return not self.unmet_prerequisites(resolved_skills)

    def build_system_prompt(self, loaded_skills: Optional[list[str]] = None) -> str:
        """构建系统提示词（渐进式技能加载两段式）。

        第一段（始终存在）：品牌 / 活跃渠道 / 权限模式 / 可用技能菜单。
        第二段（有技能加载后补充）：已加载技能的 SOP 指令段。

        Args:
            loaded_skills: 已加载的技能名列表（为 None 时表示无技能加载）。

        Returns:
            str: 系统提示词。
        """
        sources_desc = ", ".join(self._session.active_sources) if self._session.active_sources else "未配置"
        brand = self._workspace.metadata.get("brand", "OceanBreeze")
        lines = [
            f"你是 {brand} 品牌的电商运营助手。",
            f"当前活跃渠道: {sources_desc}。",
            f"权限模式: {self._session.permission_mode.name}。",
        ]
        # 技能菜单（所有已启用技能）
        menu = self._registry.list_menu()
        if menu:
            lines.append("【可用技能（调用 load_skill 加载后获取完整操作指南）】")
            for name, desc in menu:
                lines.append(f"- {name}: {desc}")
        # 已加载技能的 SOP 段
        loaded_skills = loaded_skills or []
        loaded_sections = []
        for name in loaded_skills:
            skill = self._registry.get(name)
            if skill is not None and skill.enabled:
                section = f"【已加载技能 {name}】{skill.description}"
                if skill.body:
                    section += f"\nSOP：{skill.body}"
                loaded_sections.append(section)
        if loaded_sections:
            lines.append("\n".join(loaded_sections))
        lines.append("请根据用户需求，调用 load_skill 加载对应技能，并按 SOP 使用工具完成运营任务。")
        return "\n".join(lines)