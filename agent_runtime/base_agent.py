"""
BaseAgent 执行引擎 — 基于 Template Method 模式的 Agent 核心编排。

BaseAgent 抽象类。
Python 版改用组合模式（后端作为依赖注入），而非继承 Backend 接口。
包含完整的生命周期管理：技能解析→前置条件→提示词→委托后端→工具执行循环→完成。

核心特性：
- 多轮工具调用循环（MAX_TOOL_ROUNDS=10），直到 LLM 不再调用工具
- PreToolUsePipeline 权限拦截，在工具执行前进行安全检查
- ASK 模式下阻塞式权限确认：挂起等待用户决定，批准执行、拒绝以错误结果回传 LLM
- AbortHandler 中断机制，支持用户随时取消
- BackendConfig 运行时更新，系统提示词动态注入
"""

from __future__ import annotations

import json as _json
import asyncio
import inspect
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Awaitable, Callable, Optional

from agent_backend.protocol import AgentBackend, BackendConfig, AgentCapabilities
from events.agent_event import (
    AgentEvent,
    PermissionRequestEvent,
    StatusEvent,
    CompleteEvent,
    AbortEvent,
    TypedError,
    TypedErrorEvent,
    ToolStartEvent,
    ToolResultEvent,
)
from execution.policy import get_last_outcome, set_call_context
from session.session import Session, ExecutionState
from session.workspace import Workspace
from permission.pre_tool_use import PreToolUsePipeline, PreToolUseAction, PreToolUseResult
from .turn_lifecycle import TurnLifecycle, DEFAULT_SKILL_REGISTRY as _SKILL_REG
from .abort_handler import AbortHandler
from .context_policy import ContextCompressor, context_threshold
from .memory_store import DEFAULT_MEMORY_STORE

# 最大工具调用轮数，防止无限循环
MAX_TOOL_ROUNDS = 10

# 只读工具前缀，与权限层 mode_gate 的只读判定保持一致
READONLY_TOOL_PREFIXES = ("query_", "get_", "list_", "search_")

# 权限解析器签名：接收权限请求，返回用户是否批准
PermissionResolver = Callable[[PermissionRequestEvent], Awaitable[bool]]


class BaseAgent:
    """Agent 核心编排引擎，不直接实现 AgentBackend Protocol。

    使用组合模式注入后端实例，通过 Template Method 模式管理生命周期。
    与 TypeScript 版的区别：TS 版 BaseAgent extends AgentBackend，
    Python 版 BaseAgent 使用后端的组合关系。

    用法:
        backend = create_backend(config)
        agent = BaseAgent(backend, workspace)
        agent.set_permission_pipeline(pipeline)
        agent.set_permission_resolver(resolver)
        agent.set_tool_handlers(handlers)
        async for event in agent.chat(session, "查询库存", tools):
            handle(event)
    """

    def __init__(
        self,
        backend: AgentBackend,
        workspace: Workspace,
        skill_registry=None,
    ) -> None:
        """初始化 BaseAgent。

        Args:
            backend: 后端实例（OpenAIAgent 或 AnthropicAgent）。
            workspace: 工作空间对象。
            skill_registry: 技能注册表（渐进式加载用），缺省使用内置默认注册表。
        """
        self._backend = backend
        self._workspace = workspace
        self._skill_registry = skill_registry or _SKILL_REG
        self._abort_handler = AbortHandler()
        # 压缩阈值动态化：模型上下文窗口 - 13k 安全边际（自动压缩触发线）
        # capabilities 未实现/返回 None 时退化为默认 128k 窗口
        caps = backend.capabilities() if hasattr(backend, "capabilities") else None
        window = getattr(caps, "context_window", 128000) if caps is not None else 128000
        self._compressor = ContextCompressor(threshold_chars=context_threshold(window))
        self._pipeline: Optional[PreToolUsePipeline] = None
        self._tool_handlers: dict[str, Callable] = {}
        self._tool_sources: dict[str, str] = {}  # 工具名 → 来源通道（commerce/mcp）
        self._permission_resolver: Optional[PermissionResolver] = None
        self._permission_canceller: Optional[Callable[[], None]] = None
        self._pending_tool_tasks: set[asyncio.Task] = set()
        self._trace_id: str = ""  # 当前 turn 的执行轨迹 ID（Trace 从事件流生长）
        # 渐进式技能加载状态
        self._loaded_skills: set[str] = set()  # 当前 turn 已加载的技能名
        self._load_skill_def: Optional[dict] = None  # load_skill 工具定义（含菜单）
        self._current_system_prompt: str = ""  # 当前系统提示词（跨轮更新）
        self._all_tools: list[dict] = []  # 全量工具定义

    @property
    def backend(self) -> AgentBackend:
        """获取后端实例。"""
        return self._backend

    def set_permission_pipeline(self, pipeline: PreToolUsePipeline) -> None:
        """设置权限检查管线。

        Args:
            pipeline: PreToolUsePipeline 实例，用于工具调用前的安全检查。
        """
        self._pipeline = pipeline

    def set_permission_resolver(self, resolver: PermissionResolver) -> None:
        """设置权限确认解析器。

        ASK 模式下工具调用将挂起，直到解析器返回用户决定（True 批准 / False 拒绝）。
        未设置解析器时，ASK 请求按安全默认处理（拒绝执行）。

        Args:
            resolver: 异步解析函数，签名 (PermissionRequestEvent) -> bool。
        """
        self._permission_resolver = resolver

    def set_permission_canceller(self, canceller: Callable[[], None]) -> None:
        """设置权限取消回调。

        abort() 时自动调用该回调，通知外部层（如 WebSocket）取消所有挂起的权限请求。
        避免权限解析器在 Agent 中断后仍阻塞等待。

        Args:
            canceller: 无参回调函数，应在外部层拒绝所有挂起的权限 Future。
        """
        self._permission_canceller = canceller

    def _cancel_pending_permissions(self) -> None:
        """内部：取消所有挂起的权限请求。"""
        if self._permission_canceller is not None:
            self._permission_canceller()

    def set_tool_handlers(self, handlers: dict[str, Callable], sources: Optional[dict[str, str]] = None) -> None:
        """设置工具执行处理器映射。

        Args:
            handlers: 工具名 → 处理函数 的映射字典。
                      处理函数签名: (channel, sku, ...) -> str | dict。
            sources: 工具名 → 来源通道（"commerce"/"mcp"），回填事件 source 字段。
        """
        self._tool_handlers = handlers
        self._tool_sources = dict(sources or {})

    def capabilities(self) -> AgentCapabilities:
        """获取后端能力声明。

        Returns:
            AgentCapabilities: 后端能力。
        """
        return self._backend.capabilities()

    def abort(self, reason: str) -> None:
        """中断当前执行。

        同时设置 AbortHandler、后端中断标志，取消所有挂起的权限请求，
        并取消正在执行中的工具任务。

        Args:
            reason: 中断原因。
        """
        self._abort_handler.abort(reason)
        self._backend.abort(reason)
        self._cancel_pending_permissions()
        for t in list(self._pending_tool_tasks):
            t.cancel()

    def cancel_pending_permissions(self) -> None:
        """取消所有挂起的权限请求，解除 Agent 的等待阻塞。

        权限解析器（如 WebSocket 层的 Future）应通过此方法被通知取消。
        未设置取消回调时无操作。
        """
        self._cancel_pending_permissions()

    def compact_session(self, session: Session) -> bool:
        """手动压缩当前会话：早期消息折叠为结构化摘要，写回会话。

        Args:
            session: 目标会话，其 messages 会被折叠并替换。

        Returns:
            bool: 是否发生了实际压缩。
        """
        new_entries = self._compressor.collapse_session_entries(session.messages)
        if len(new_entries) == len(session.messages):
            return False
        session.messages = new_entries
        return True

    # ------------------------------------------------------------------
    # chat() 主流程 — 拆分为阶段方法以降低方法体复杂度
    # ------------------------------------------------------------------

    async def chat(
        self, session: Session, user_message: str, tools: list[dict]
    ) -> AsyncGenerator[AgentEvent, None]:
        """执行一个完整的 turn，包含工具调用循环。

        Template Method 五阶段，每个阶段调用独立方法：
        1. 技能解析 + 工具过滤
        2. 前置条件检查
        3. 系统提示词构建 + 长期记忆注入
        4. LLM 后端执行（事件循环 + 权限处理）
        5. 工具执行 + 消息装配 + 完成

        Args:
            session: 当前会话对象。
            user_message: 用户输入的消息文本。
            tools: 工具定义列表（OpenAI/Anthropic 格式）。

        Yields:
            AgentEvent: 流式事件（StatusEvent、TextDeltaEvent、ToolStartEvent 等）。
        """
        lifecycle = TurnLifecycle(
            session, self._workspace, self._backend, registry=self._skill_registry
        )
        session.execution_state = ExecutionState.RUNNING
        self._lifecycle = lifecycle
        self._trace_id = uuid.uuid4().hex[:12]
        # 真实模型成本维度：每回合清零后端 token 累计
        if hasattr(self._backend, "reset_usage"):
            self._backend.reset_usage()

        # 渐进式技能加载：重置本 turn 加载状态，构建 load_skill 菜单工具
        self._loaded_skills = set()
        self._all_tools = list(tools)
        self._load_skill_def = self._build_load_skill_def()
        self._current_system_prompt = self._build_system_prompt_text(
            lifecycle, session, user_message
        )
        config = self._backend.get_config()
        config.system_prompt = self._current_system_prompt
        self._backend.update_runtime_config(config)

        # /命令显式指定技能（"/price_management 调价"）：跳过模型"看菜单点菜"，
        # 直接预加载 SOP——显式指定优先于模型自主选择，加载失败不阻塞（回落菜单流程）
        preload = self._parse_slash_skill(user_message)
        if preload:
            self._loaded_skills.add(preload)
            self._current_system_prompt = self._build_system_prompt_text(
                lifecycle, session, user_message
            )
            config = self._backend.get_config()
            config.system_prompt = self._current_system_prompt
            self._backend.update_runtime_config(config)
            yield StatusEvent(message=f"已按 /{preload} 预加载技能")

        yield StatusEvent(message=f"可用技能: {self._skill_registry.list_menu_names()}")

        session.add_message("user", user_message)
        messages = self._prepare_messages(session)

        messages, compressed = self._compressor.collapse(messages)
        if compressed:
            sunk = ""
            try:
                for m in messages:
                    if m.get("role") == "system" and str(m.get("content", "")).startswith("[会话摘要"):
                        ws_id = session.workspace.workspace_id
                        # 压缩-记忆联动：摘要易失（再次压缩会被卷走），决策持久
                        # （沉淀进记忆）；固定命名使反复压缩经整合层覆盖为最新一份
                        DEFAULT_MEMORY_STORE.remember(
                            ws_id, f"会话摘要_{ws_id}\n{m['content']}", "project"
                        )
                        sunk = "，决策已沉淀至长期记忆"
                        break
            except Exception:
                sunk = ""  # 联动失败不阻塞主链路
            yield StatusEvent(message=f"上下文超限，已压缩早期会话为摘要{sunk}")

        yield StatusEvent(message="委托后端执行...")

        # 阶段 4-5: LLM 执行循环 + 工具执行 + 消息装配
        async for event in self._execute_loop(session, messages, user_message):
            yield event

    # ------------------------------------------------------------------
    # 渐进式技能加载（Progressive Skill Loading）
    # ------------------------------------------------------------------

    def _parse_slash_skill(self, user_message: str) -> Optional[str]:
        """解析 "/技能名 ..." 前缀：命中已启用且前置条件满足的技能则返回名称。

        / 命令是渐进式加载的显式入口（用户点名 > 模型猜）；名称无效、
        技能停用或前置条件不满足时返回 None，回落到模型菜单选择流程。
        """
        m = re.match(r"^/([A-Za-z_][A-Za-z0-9_]*)", (user_message or "").strip())
        if not m:
            return None
        skill = self._skill_registry.get(m.group(1))
        if skill is None or not skill.enabled:
            return None
        lifecycle = getattr(self, "_lifecycle", None)
        if lifecycle is not None and lifecycle.unmet_prerequisites([skill.name]):
            return None
        return skill.name

    def _build_load_skill_def(self) -> Optional[dict]:
        """构造 load_skill 元工具定义（唯一始终可见的工具）。

        description 里携带技能菜单（name: description），让模型「先看菜单再点菜」。
        """
        reg = self._skill_registry
        menu = reg.list_menu()
        if not menu:
            return None
        menu_lines = "\n".join(f"- {name}: {desc}" for name, desc in menu)
        return {
            "name": "load_skill",
            "description": (
                "加载指定技能的完整操作指南（SOP）。\n"
                f"可用技能菜单：\n{menu_lines}\n"
                "调用时传入技能名；加载成功后按 SOP 执行运营任务。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的技能名（从菜单选择）",
                        "enum": [name for name, _ in menu],
                    }
                },
                "required": ["skill_name"],
            },
        }

    def _build_system_prompt_text(
        self, lifecycle: TurnLifecycle, session: Session, user_message: str
    ) -> str:
        """构建当前系统提示词（含技能菜单 + 已加载技能的 SOP 段 + 记忆注入）。"""
        system_prompt = lifecycle.build_system_prompt(list(self._loaded_skills))
        mem_section = DEFAULT_MEMORY_STORE.recall_section(
            session.workspace.workspace_id, user_message
        )
        if mem_section:
            system_prompt += "\n\n" + mem_section
        return system_prompt

    def _visible_tools(self) -> list[dict]:
        """计算当前轮次对 LLM 可见的工具集。

        技能是纯知识（SOP/平台规则），不绑定工具：全部业务工具常可见，
        渐进式披露只作用于知识注入（load_skill 命中才注入 SOP 正文）。
        """
        visible: list[dict] = []
        if self._load_skill_def:
            visible.append(self._load_skill_def)
        visible.extend(self._all_tools)
        return visible

    async def _handle_load_skill(
        self, event: ToolStartEvent
    ) -> ToolResultEvent:
        """执行 load_skill 元工具：校验技能 → 检查前置条件 → 标记已加载。

        加载成功后刷新系统提示词（追加 SOP 段），后续轮次暴露该技能的工具。
        """
        skill_name = str((event.input or {}).get("skill_name", "")).strip()
        reg = self._skill_registry
        skill = reg.get(skill_name) if skill_name else None
        if skill is None:
            return ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name="load_skill",
                result=f"技能 {skill_name!r} 不存在，可用技能: {', '.join(reg.list_menu_names())}",
                is_error=True,
            )
        if not skill.enabled:
            return ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name="load_skill",
                result=f"技能 {skill_name} 已停用，无法加载",
                is_error=True,
            )
        lifecycle = getattr(self, "_lifecycle", None)
        if lifecycle is not None:
            unmet = lifecycle.unmet_prerequisites([skill_name])
            if unmet:
                return ToolResultEvent(
                    tool_use_id=event.tool_use_id,
                    tool_name="load_skill",
                    result="；".join(unmet),
                    is_error=True,
                )
        self._loaded_skills.add(skill_name)
        # 加载后刷新系统提示词（SOP 段追加），供后续轮次使用
        self._current_system_prompt = self._build_system_prompt_text(
            lifecycle, lifecycle._session, ""
        ) if lifecycle is not None else self._current_system_prompt
        return ToolResultEvent(
            tool_use_id=event.tool_use_id,
            tool_name="load_skill",
            result=(
                f"已加载技能 {skill_name}（{skill.description}）。\n"
                f"SOP：{skill.body or '按描述执行'}"
            ),
            is_error=False,
        )

    def _prepare_messages(self, session: Session) -> list[dict]:
        """将 session.messages 转换为 LLM API 所需的消息格式。"""
        messages = []
        for m in session.messages:
            entry = {"role": m["role"], "content": m["content"]}
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            messages.append(entry)
        return messages

    async def _execute_loop(
        self,
        session: Session,
        messages: list[dict],
        user_message: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """阶段 4-5: LLM 执行循环 + 工具执行 + 消息装配。

        渐进式技能加载：每轮开始时计算「当前可见工具」= load_skill（恒有）+
        已加载技能绑定的工具；并把最新系统提示词（含已加载技能的 SOP 段）
        作为首条消息注入，保证模型感知本轮的技能上下文。
        """
        round_count = 0
        while round_count < MAX_TOOL_ROUNDS:
            if self._abort_handler.is_aborted:
                session.execution_state = ExecutionState.ABORTED
                break
            round_count += 1
            # 幂等键上下文（session/turn）：后续工具任务经 contextvars 继承
            set_call_context(session.session_id, round_count)

            visible_tools = self._visible_tools()
            # 系统提示词注入为 messages 首条（每轮携带最新技能上下文）。
            # 直接原地插入（而非副本），保证回合内追加的 tool 结果对 backend
            # 可见；回合结束后移除，避免污染会话历史。
            messages.insert(0, {"role": "system", "content": self._current_system_prompt})

            pending_tools: list[ToolStartEvent] = []
            round_tools: list[ToolStartEvent] = []
            round_results: dict[str, ToolResultEvent] = {}
            assistant_text = ""

            # 子阶段 A: 委托 LLM 后端执行，流式 yield 事件给调用方
            try:
                async for event in self._backend.chat(messages, visible_tools, session.session_id):
                    if self._abort_handler.is_aborted:
                        session.execution_state = ExecutionState.ABORTED
                        break

                    if event.type == "text_delta":
                        assistant_text += event.text
                        yield event

                    elif event.type == "tool_start":
                        round_tools.append(event)
                        async for yielded in self._handle_tool_start_permission(
                            session, event, pending_tools, round_results
                        ):
                            yield yielded

                    else:
                        yield event
            finally:
                # 移除本轮注入的 system 消息（历史只保留 user/assistant/tool）
                if messages and messages[0].get("role") == "system":
                    messages.pop(0)

            # 子阶段 B: 执行获批的工具调用（每完成一个即 yield，让调用方可感知进度后 abort）
            async for result in self._execute_pending_tools(session, pending_tools, round_results):
                yield result

            # 子阶段 C: 消息装配
            self._assemble_turn_messages(
                session, messages, round_tools, round_results, assistant_text
            )

            if not pending_tools:
                break

        if session.execution_state == ExecutionState.ABORTED:
            yield AbortEvent(reason=self._abort_handler.reason)
        else:
            session.execution_state = ExecutionState.COMPLETED
            action, name = DEFAULT_MEMORY_STORE.handle_turn(
                session.workspace.workspace_id, user_message, messages
            )
            if action in ("store", "updated", "forgot"):
                yield StatusEvent(message=f"长期记忆：{action} {name}")
            yield CompleteEvent()

    async def _handle_tool_start_permission(
        self,
        session: Session,
        event: ToolStartEvent,
        pending_tools: list[ToolStartEvent],
        round_results: dict[str, ToolResultEvent],
    ) -> AsyncGenerator[AgentEvent, None]:
        """权限三路分发：BLOCK/ASK/ALLOW，流式 yield 事件。"""
        # Trace 从事件流上自然生长：工具事件统一携带轨迹 ID 与来源通道
        event.trace_id = self._trace_id
        event.source = self._tool_sources.get(event.tool_name, "")
        perm_result = self._check_permission(event)

        if perm_result.action == PreToolUseAction.BLOCK:
            # 先发 tool_start 让调用方可见"尝试了什么"，紧跟错误结果展示拦截原因
            yield event
            denied = ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                result=f"[拦截] {perm_result.reason}" if perm_result.reason else "[拦截] 该操作被安全规则阻止",
                is_error=True,
                trace_id=event.trace_id,
                source=event.source,
            )
            round_results[event.tool_use_id] = denied
            yield denied
            return

        if perm_result.action == PreToolUseAction.ASK:
            async for yielded in self._handle_ask_permission(
                session, event, perm_result, pending_tools, round_results
            ):
                yield yielded
            return

        # ALLOW
        pending_tools.append(event)
        yield event

    async def _handle_ask_permission(
        self,
        session: Session,
        event: ToolStartEvent,
        perm_result: PreToolUseResult,
        pending_tools: list[ToolStartEvent],
        round_results: dict[str, ToolResultEvent],
    ) -> AsyncGenerator[AgentEvent, None]:
        """ASK 权限模式：发起权限请求 -> 等待用户决定 -> 批准执行 / 拒绝。"""
        yield event  # 先展示工具预览卡
        request = PermissionRequestEvent(
            request_id=f"perm_{event.tool_use_id}",
            tool_name=event.tool_name,
            tool_input=event.input,
            reason=perm_result.reason or "该操作将产生业务副作用，需要用户确认",
        )
        session.permission_requests.append({
            "request_id": request.request_id,
            "tool_name": event.tool_name,
            "input": event.input,
            "user": session.user,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        session.execution_state = ExecutionState.WAITING_PERMISSION
        yield request

        approved = await self._resolve_permission(request)

        if self._abort_handler.is_aborted:
            session.execution_state = ExecutionState.ABORTED
            refused = ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                result="[已中断] 执行被中断",
                is_error=True,
                trace_id=event.trace_id,
                source=event.source,
            )
            round_results[event.tool_use_id] = refused
            yield refused
            return

        session.execution_state = ExecutionState.RUNNING
        session.permission_requests[-1]["approved"] = approved

        if not approved:
            refused = ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                result="[已拒绝] 用户拒绝了该操作的执行",
                is_error=True,
                trace_id=event.trace_id,
                source=event.source,
            )
            round_results[event.tool_use_id] = refused
            yield refused
            return

        # 已批准：加入执行队列
        pending_tools.append(event)

    async def _execute_pending_tools(
        self,
        session: Session,
        pending_tools: list[ToolStartEvent],
        round_results: dict[str, ToolResultEvent],
    ) -> AsyncGenerator[ToolResultEvent, None]:
        """子阶段 B: 并发执行获批的工具调用，每完成一个即 yield 结果事件。

        调用方可通过 yield 的 ToolResultEvent 及时感知执行进度，
        并在收到特定结果后调用 abort() 中断剩余任务。
        """
        if not pending_tools:
            return

        tasks = {asyncio.create_task(self._run_tool(ts)): ts for ts in pending_tools}
        for t in tasks:
            self._pending_tool_tasks.add(t)
        remaining = set(tasks)
        while remaining:
            if self._abort_handler.is_aborted:
                session.execution_state = ExecutionState.ABORTED
                for t in remaining:
                    t.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
                break
            done, remaining = await asyncio.wait(
                remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                ts, result = t.result()
                round_results[ts.tool_use_id] = result
                self._pending_tool_tasks.discard(t)
                # 立即 yield 刚完成的结果，让调用方感知进度
                yield result
        # 清理已完成任务
        self._pending_tool_tasks = {t for t in self._pending_tool_tasks if not t.done()}

    def _assemble_turn_messages(
        self,
        session: Session,
        messages: list[dict],
        round_tools: list[ToolStartEvent],
        round_results: dict[str, ToolResultEvent],
        assistant_text: str,
    ) -> None:
        """子阶段 C: 将本轮 assistant 消息与工具结果写入消息历史。"""
        if not round_tools:
            if assistant_text:
                session.add_message("assistant", assistant_text)
            return

        assistant_msg = {
            "role": "assistant",
            "content": assistant_text,
            "tool_calls": [
                {
                    "id": t.tool_use_id,
                    "type": "function",
                    "function": {
                        "name": t.tool_name,
                        "arguments": _json.dumps(t.input, ensure_ascii=False),
                    },
                }
                for t in round_tools
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        messages.append(assistant_msg)
        session.add_message_record(assistant_msg)

        for t in round_tools:
            r = round_results.get(t.tool_use_id)
            result_text = r.result if r is not None else "[无结果]"
            messages.append({
                "role": "tool",
                "tool_call_id": t.tool_use_id,
                "content": result_text,
            })
            session.add_message_record({
                "role": "tool",
                "tool_call_id": t.tool_use_id,
                "content": result_text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            session.add_tool_call({
                "tool_name": t.tool_name,
                "tool_use_id": t.tool_use_id,
                "input": t.input,
                "result": result_text,
                "is_error": bool(r.is_error) if r is not None else True,
                "user": session.user,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    async def _resolve_permission(self, request: PermissionRequestEvent) -> bool:
        """等待用户对权限请求作出决定。

        未设置解析器或解析过程异常时，按安全默认返回 False（拒绝执行）。

        Args:
            request: 权限请求事件。

        Returns:
            bool: 用户是否批准执行。
        """
        if self._permission_resolver is None:
            return False
        try:
            return await self._permission_resolver(request)
        except Exception:
            return False

    def _check_permission(self, event: ToolStartEvent) -> PreToolUseResult:
        """通过 PreToolUsePipeline 检查工具调用权限。

        如果未设置管线（pipeline 为 None），默认放行所有操作。

        Args:
            event: 工具调用事件。

        Returns:
            PreToolUseResult: 权限检查结果。
        """
        if self._pipeline is None:
            return PreToolUseResult(action=PreToolUseAction.ALLOW)
        return self._pipeline.check(event.tool_name, event.input)

    async def _run_tool(self, event: ToolStartEvent) -> tuple[ToolStartEvent, ToolResultEvent]:
        """执行单个工具并返回 (事件, 结果)，供并发调度按 Task 归位。

        Args:
            event: 工具调用事件。

        Returns:
            (ToolStartEvent, ToolResultEvent): 原始事件与执行结果。
        """
        result = await self._execute_tool(event)
        return event, result

    async def _execute_tool(self, event: ToolStartEvent) -> ToolResultEvent:
        """执行单个工具调用，返回结果事件（统一回填 Trace/策略元数据）。

        Execution Policy 的执行元数据（尝试次数/耗时/幂等回放）经
        contextvar 从共享执行通道带出，回填到结果事件——Trace 可还原
        重试链路，评测可断言「429 重试 2 次后成功」这类异常路径契约。
        """
        if event.tool_name == "load_skill":
            result = await self._handle_load_skill(event)
        else:
            result = await self._execute_tool_inner(event)
        result.trace_id = event.trace_id
        result.source = event.source
        return result

    async def _execute_tool_inner(self, event: ToolStartEvent) -> ToolResultEvent:
        """执行单个工具调用的本体（handler 分发 + 异常归一）。"""
        handler = self._tool_handlers.get(event.tool_name)
        if handler is None:
            return ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                result=f"工具 {event.tool_name} 未注册",
                is_error=True,
            )
        try:
            if inspect.iscoroutinefunction(handler):
                result = await handler(**event.input)
            else:
                result = handler(**event.input)
            result_event = ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                result=str(result),
                is_error=False,
            )
            outcome = get_last_outcome()
            if outcome is not None:
                result_event.attempt = outcome.attempts
                result_event.duration_ms = outcome.duration_ms
                result_event.idempotent_replay = outcome.idempotent_replay
            return result_event
        except Exception as e:
            return ToolResultEvent(
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                result=str(e),
                is_error=True,
            )