"""
共享状态 — 模块级全局变量与辅助函数，供 server.py 和 router 各模块共享。

职责：持有会话/工作空间/MCP 客户端池等全局状态，以及会话持久化、Agent 构建、
后端配置等辅助函数。避免在路由模块间相互 import 造成循环依赖。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

from session.session import Session, PermissionMode
from session.workspace import Workspace
from session.storage import save_session, load_session_full
from agent_backend.protocol import BackendConfig
from agent_backend.factory import create_backend
from agent_backend.provider_registry import DEFAULT_PROVIDER_REGISTRY
from agent_runtime.base_agent import BaseAgent
from sources import builtin_tools
from integrations.mcp.client_pool import McpClientPool
from permission.pre_tool_use import PreToolUsePipeline
from permission.rule_engine import mode_gate_rule, workspace_rules_rule
from permission.rbac import role_gate_rule

# ---------------------------------------------------------------------------
# 全局 MCP 客户端池 & 生命周期
# ---------------------------------------------------------------------------

# 工具三通道：内置平台 API 工具（builtin_tools，进程内直连 REST/Adapter）
# + MCP 外部工具（mcp_pool，用户配置接入的第三方工具服务）。
mcp_pool = McpClientPool()

_channel_api_proc: Optional[subprocess.Popen] = None

# ---------------------------------------------------------------------------
# 会话 & 工作空间存储
# ---------------------------------------------------------------------------

sessions: dict[str, Session] = {}
workspaces: dict[str, Workspace] = {}

# 该会话当前活跃的 WebSocket 连接（URL 中 session_id → 连接集合），
# 供模式切换等 REST/WS 操作向该会话广播 mode_change 事件。
session_ws_clients: dict[str, set] = {}

# ---------------------------------------------------------------------------
# 跨会话审批注册表（审批中心）
# ---------------------------------------------------------------------------

# request_id → {future, session_id, tool_name, tool_input, reason, timestamp}。
# WS 权限解析器挂起时登记，审批中心 REST 端点据此聚合待审批列表并经
# future.set_result 唤醒对应挂起的 turn；连接断开/turn 结束时移除。
APPROVAL_REGISTRY: dict[str, dict] = {}

_STORAGE_DIR = os.environ.get("AGENT_STORAGE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "sessions")
)


def _start_channel_api() -> Optional[subprocess.Popen]:
    """拉起 mock 渠道平台服务（独立进程，REST 端点，业务数据 Mock）。"""
    global _channel_api_proc
    port = int(os.environ.get("CHANNEL_API_PORT", "18080"))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "mock_commerce.routes", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=0.5):
                    break
            except Exception:
                time.sleep(0.1)
        _channel_api_proc = proc
        return proc
    except Exception as exc:
        print(f"[WARN] mock 渠道平台服务启动失败: {exc}")
        return None


def _stop_channel_api() -> None:
    """停止 mock 渠道平台服务。"""
    global _channel_api_proc
    if _channel_api_proc is not None:
        try:
            _channel_api_proc.terminate()
        except Exception:
            pass
        _channel_api_proc = None


# ---------------------------------------------------------------------------
# 工作空间
# ---------------------------------------------------------------------------


def _default_workspace() -> Workspace:
    """获取或创建默认工作空间。"""
    return _get_workspace_or_create("default")


def _get_workspace_or_create(workspace_id: str) -> Workspace:
    """按 id 获取工作空间，不存在则惰性创建（多品牌隔离的注册表入口）。

    除 default 外的工作空间在首次引用时创建，含默认品牌名与基础规则；
    支持会话按 workspace_id 路由，记忆（data/memory/<ws>/）与规则随 workspace 隔离。
    """
    workspace_id = workspace_id or "default"
    ws = workspaces.get(workspace_id)
    if ws is not None:
        return ws
    if workspace_id == "default":
        ws = Workspace(
            workspace_id="default",
            name="OceanBreeze",
            metadata={"brand": "OceanBreeze"},
            rules=[{"type": "price_above_cost"}],
            credentials=["taobao_oauth", "jd_oauth", "douyin_oauth"],
        )
    else:
        ws = Workspace(
            workspace_id=workspace_id,
            name=workspace_id,
            metadata={"brand": workspace_id},
            rules=[{"type": "price_above_cost"}],
        )
    workspaces[workspace_id] = ws
    return ws


# ---------------------------------------------------------------------------
# 会话持久化
# ---------------------------------------------------------------------------


def _persist(session: Session) -> None:
    """将会话消息增量持久化到 JSONL 文件。"""
    try:
        save_session(session, _STORAGE_DIR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 权限模式切换 & 广播（供 REST / WS 双通道共用，避免 server/router 循环 import）
# ---------------------------------------------------------------------------

_VALID_MODES = {"READONLY", "ASK", "EXECUTE"}


def validate_mode(mode: str) -> str:
    """校验权限模式名，非法时抛 ValueError。"""
    if mode not in _VALID_MODES:
        raise ValueError(f"非法权限模式: {mode}，应为 READONLY / ASK / EXECUTE")
    return mode


async def broadcast_mode(session_id: str, mode: str) -> None:
    """向该会话全部活跃 WebSocket 连接推送 mode_change 事件（同类连接去重）。"""
    clients = session_ws_clients.get(session_id)
    if not clients:
        return
    for ws in list(clients):
        try:
            await ws.send_json({"type": "mode_change", "mode": mode})
        except Exception:
            # 单连接推送失败不阻断其它连接，交由断连清理。
            pass


def apply_mode_change(session: Session, mode: str) -> str:
    """更新会话权限模式并持久化（不广播，由调用方决定要不要发）。

    session.permission_mode 由运行时管线实时读取（mode_gate_rule 用 callable 闭包），
    因此同一条 WS 连接上切换即时生效，无需重建 agent。
    """
    validate_mode(mode)
    session.permission_mode = PermissionMode[mode]
    _persist(session)
    return mode


def _restore_from_disk() -> None:
    """扫描持久化目录，将磁盘上存在而内存中没有的会话恢复到内存。"""
    if not os.path.isdir(_STORAGE_DIR):
        return
    for fname in os.listdir(_STORAGE_DIR):
        if not fname.endswith(".jsonl"):
            continue
        session_id = fname[: -len(".jsonl")]
        if session_id in sessions:
            continue
        restored = Session(
            session_id=session_id,
            workspace=_default_workspace(),
            permission_mode=PermissionMode.ASK,
            active_sources=["taobao", "jd", "douyin"],
        )
        try:
            stored = load_session_full(session_id, _STORAGE_DIR)
            if stored.user:
                restored.user = stored.user
            if stored.model_state:
                restored.model_state = stored.model_state
            if stored.permission_mode and stored.permission_mode in {"READONLY", "ASK", "EXECUTE"}:
                restored.permission_mode = PermissionMode[stored.permission_mode]
            if stored.workspace_id:
                restored.workspace = _get_workspace_or_create(stored.workspace_id)
            for sm in stored.messages:
                entry = {"role": sm.role, "content": sm.content, "timestamp": sm.timestamp}
                if sm.tool_calls:
                    entry["tool_calls"] = sm.tool_calls
                if sm.tool_call_id:
                    entry["tool_call_id"] = sm.tool_call_id
                restored.messages.append(entry)
            restored.tool_calls = stored.tool_calls
            restored.permission_requests = stored.permission_requests
            restored.mark_saved()
        except (OSError, ValueError, KeyError):
            continue
        sessions[session_id] = restored


# ---------------------------------------------------------------------------
# 后端配置 & Agent 构建
# ---------------------------------------------------------------------------


def _build_tools() -> list[dict]:
    """工具定义合并：内置平台 API 工具 + MCP 外部工具（同名内置优先）。"""
    defs = builtin_tools.get_definitions()
    builtin_names = {d["name"] for d in defs}
    defs.extend(
        d for d in mcp_pool.get_all_tool_definitions() if d.get("name") not in builtin_names
    )
    return defs


def _backend_provider_name() -> str:
    """当前后端 provider 名（持久化激活 > AGENT_BACKEND env > 默认，与 build_config 一致）。"""
    active = DEFAULT_PROVIDER_REGISTRY.get(DEFAULT_PROVIDER_REGISTRY.effective_provider_name())
    if active is not None:
        return str(active.get("provider") or "openai")
    return os.environ.get("AGENT_BACKEND", "openai").lower()


def _backend_model_name() -> str:
    """当前后端模型名（优先激活供应商 default_model，回退环境变量/内置默认）。"""
    active = DEFAULT_PROVIDER_REGISTRY.get(DEFAULT_PROVIDER_REGISTRY.effective_provider_name())
    if active is not None:
        model = str(active.get("default_model") or "").strip()
        if model:
            return model
    env_model = os.environ.get("AGENT_MODEL")
    if env_model:
        return env_model
    provider = _backend_provider_name()
    return "claude-3-5-sonnet-latest" if provider == "anthropic" else "gpt-4o-mini"


def _build_backend_config() -> BackendConfig:
    """构建后端配置：由全局注册表当前激活供应商决定，回退环境变量/内置默认。

    HARNESS_PROVIDER 覆盖：评测 Harness 真实模型模式按指定供应商构建
    （不改动用户 providers.json 的激活状态）。
    """
    override = os.environ.get("HARNESS_PROVIDER", "").strip()
    if override:
        return DEFAULT_PROVIDER_REGISTRY.build_config(override)
    return DEFAULT_PROVIDER_REGISTRY.build_config()


def _build_agent(session: Session) -> BaseAgent:
    """构建默认的 Agent 实例，包含预置的淘宝、京东、抖音渠道 Source。"""
    backend = create_backend(_build_backend_config())
    workspace = session.workspace
    agent = BaseAgent(backend, workspace)

    # 模式以 callable 传入：每次工具调用实时读取 session.permission_mode，
    # 使同一条 WS 连接上的模式切换即时生效（不需要重建 agent / 管线）。
    pipeline = PreToolUsePipeline()
    pipeline.add_checker(role_gate_rule(session.user["role"]))
    pipeline.add_checker(workspace_rules_rule(session.workspace.rules))
    pipeline.add_checker(mode_gate_rule(lambda: session.permission_mode.name))
    agent.set_permission_pipeline(pipeline)

    # handler 合并：内置平台 API 工具优先，MCP 外部工具补充；
    # 来源通道映射随 handler 一并注入（事件 source 字段 = commerce/mcp）
    mcp_handlers = mcp_pool.get_all_handlers()
    handlers = {**mcp_handlers, **builtin_tools.get_handlers()}
    sources = {**{n: "mcp" for n in mcp_handlers}, **{n: "commerce" for n in builtin_tools.get_handlers()}}
    agent.set_tool_handlers(handlers, sources)
    return agent