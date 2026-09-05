"""运行器 — 在独立（非 pytest）进程中驱动完整业务链路。

复用生产装配：mcp_pool 真实 stdio 子进程 + AGENT_BACKEND=mock 剧本后端
+ PreToolUse 真实权限管线 + platform=mock 离线 ASGI 兜底。

约束：MCP 的 stdio_client/ClientSession 绑定创建时的事件循环，跨
asyncio.run 复用会触发 anyio 的 cancel-scope 错误；因此单 case 的
「连接 → 建 agent → 全部对话轮 → 关闭」必须在同一次 run 内完成，
case 之间天然以独立事件循环隔离（顺带获得 case 级状态隔离）。

env 隔离：每次 case 使用独立环境目录，AGENT_STORAGE_DIR /
CHANNEL_CONFIG_FILE / MEMORY_DIR / PROVIDER_CONFIG_FILE 指向该目录，
不污染仓库 data/；结束时恢复进程 env 快照（注册表/记忆单例为进程级，
CLI 单进程运行无需恢复）。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Optional

from harness.assertions import assert_step
from harness.hooks import get_after_hook, get_setup_hook
from harness.metrics import CaseRecord
from harness.recorder import Recorder, _serialize_event

_ENV_KEYS = (
    "AGENT_STORAGE_DIR",
    "CHANNEL_CONFIG_FILE",
    "MEMORY_DIR",
    "PROVIDER_CONFIG_FILE",
    "CHANNEL_API_AUTO",
    "AGENT_BACKEND",
    "SKILLS_DIR",
    "MCP_SERVERS_CONFIG_FILE",
)


class Runner:
    """评测运行器：env 隔离 + 生产装配驱动 + trace 记录。"""

    def __init__(self, recorder: Recorder, run_id: str) -> None:
        self.recorder = recorder
        self.run_id = run_id
        self._snapshot: dict[str, Optional[str]] = {}

    # -- 环境隔离 ----------------------------------------------------------

    def _snapshot_env(self) -> None:
        self._snapshot = {k: os.environ.get(k) for k in _ENV_KEYS}

    def _restore_env(self) -> None:
        for k, v in self._snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _isolate(self, env_dir: Path) -> None:
        """注册表缓存与记忆单例重置到隔离目录（对齐 e2e 的隔离语义）。"""
        os.environ["AGENT_STORAGE_DIR"] = str(env_dir / "sessions")
        os.environ["CHANNEL_CONFIG_FILE"] = str(env_dir / "channels.json")
        os.environ["MEMORY_DIR"] = str(env_dir / "memory")
        os.environ["PROVIDER_CONFIG_FILE"] = str(env_dir / "providers.json")
        os.environ["CHANNEL_API_AUTO"] = "0"  # 关闭平台子进程自动拉起：离线 ASGI 兜底足够
        os.environ["AGENT_BACKEND"] = "mock"  # 剧本后端：确定性输出
        os.environ["SKILLS_DIR"] = str(env_dir / "skills")  # 用户技能隔离（save_skill 落盘处）

        # 技能注册表单例在 import 时构造，显式重定向用户目录并强制重载
        import sources.skill_registry as _sr

        _sr.DEFAULT_SKILL_REGISTRY._user_dir = env_dir / "skills"
        _sr.DEFAULT_SKILL_REGISTRY._cached_mtime = -1

        import sources.channel_registry as _cr
        import agent_backend.provider_registry as _pr

        _cr._store._mtime = -1
        _cr._store._channels = None
        _cr.DEFAULT_CHANNEL_REGISTRY._clients.clear()

        from agent_runtime import memory_store as _ms

        fresh = _ms.MemoryStore(root=str(env_dir / "memory"))
        import agent_runtime.base_agent as _ba

        _ba.DEFAULT_MEMORY_STORE = fresh
        _ms.DEFAULT_MEMORY_STORE = fresh

        _pr._store._mtime = -1
        _pr._store._providers = None
        _pr._store._active = "openai"

    # -- 单 case 执行 ------------------------------------------------------

    def run_case(
        self,
        scenario: dict[str, Any],
        timeout: float = 120.0,
        retries: int = 0,
    ) -> CaseRecord:
        """执行单个场景；超时/失败可按 retries 重跑，任一通过即判通过。"""
        name = scenario["name"]
        start = time.monotonic()
        failures: list[dict] = []
        attempts = 0
        final_status = "error"
        final_path: Optional[str] = None

        self._snapshot_env()
        try:
            for attempt in range(1, retries + 2):
                attempts = attempt
                try:
                    status, detail, trace_path = self._run_once(scenario, timeout)
                    final_path = str(trace_path) if trace_path else None
                    if status == "passed":
                        final_status = "passed"
                        break
                    failures.append(detail)
                except Exception as exc:  # 防御：_run_once 内部已分类，此处兜底
                    failures.append({"kind": "unexpected", "detail": str(exc)})
                final_status = "failed" if final_status == "error" else final_status
        finally:
            self._restore_env()

        duration = time.monotonic() - start
        return CaseRecord(
            name=name,
            status=final_status,
            duration=round(duration, 3),
            attempts=attempts,
            failures=failures,
            trace_path=final_path,
        )

    def _run_once(self, scenario: dict[str, Any], timeout: float):
        """单次完整链路运行；返回 (status, failure_detail, trace_path)。"""
        env_dir = self.recorder.case_dir(self.run_id, scenario["name"]) / "_env"
        self._isolate(env_dir)

        async def _main() -> list[dict]:
            from transport.state import mcp_pool, _build_agent, _build_tools

            await mcp_pool.connect()
            try:
                from session.session import PermissionMode, Session
                from session.workspace import Workspace

                ws = Workspace(
                    workspace_id="default",
                    name="OceanBreeze",
                    metadata={"brand": "OceanBreeze"},
                    rules=[{"type": "price_above_cost"}],
                )
                session = Session(
                    session_id="harness",
                    workspace=ws,
                    permission_mode=PermissionMode.EXECUTE,
                    active_sources=["taobao", "jd", "douyin"],
                    # 六步链路含改价/促销/上下架/工单，用 manager（店长）保证 ACL 全覆盖
                    user={"user_id": "harness_user", "role": "manager"},
                )
                agent = _build_agent(session)

                hook = get_setup_hook(scenario.get("setup"))
                if hook:
                    hook()

                raw_events: list[Any] = []
                for step in scenario["steps"]:
                    tools = _build_tools()
                    events = [ev async for ev in agent.chat(session, step["message"], tools)]
                    raw_events.extend(events)
                    assert_step(scenario["name"], step, events)

                after = get_after_hook(scenario.get("after"))
                if after:
                    after()

                return [_serialize_event(ev) for ev in raw_events]
            finally:
                await mcp_pool.close()

        async def _timed() -> list[dict]:
            return await asyncio.wait_for(_main(), timeout=timeout)

        try:
            trace = asyncio.run(_timed())
        except asyncio.TimeoutError:
            return "timeout", {"kind": "timeout", "detail": f"超过 {timeout:.0f}s 未完成"}, None
        except AssertionError as exc:
            return "failed", {"kind": "assertion", "detail": str(exc)}, None

        path = self.recorder.write_trace(self.run_id, scenario["name"], trace)
        self.recorder.write_outcome(
            self.run_id,
            scenario["name"],
            {
                "status": "passed",
                "scenario": scenario["name"],
                "events": len(trace),
                "trace": path.name,
            },
        )
        return "passed", {"kind": "none", "detail": ""}, path