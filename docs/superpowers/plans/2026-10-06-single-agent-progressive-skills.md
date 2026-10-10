# 单业务 Agent 与 Skill 按需加载 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收口 Workspace 唯一商品运营 Agent 的产品入口，引入无独立 Router 的 Skill 目录与按需加载，并把商品组合根移出 `adapters`。

**Architecture:** Session 创建由服务端解析 Workspace 唯一有效 AgentVersion；Runtime 继续保留通用 Agent 版本模型。Run 创建时固定可见 Skill 版本，模型先看到 Skill 元数据，通过 Runtime 内置只读 `load_skill` 激活完整指令；激活事实写入 AgentEvent，恢复时由事件重建。`composition/` 作为唯一产品组合根，`runtime/`、`agents/` 和 `adapters/` 保持单向依赖。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、PostgreSQL、React 19、TypeScript、pytest、Vitest、Ruff、mypy。

**脏工作区保护：** 当前仓库已有大量未提交改动，且与本计划涉及文件存在重叠。实施时先保存 `git status --short` 基线，只用 `apply_patch` 做定点修改；每个任务用精确路径的 `git diff -- <paths>` 复核，不执行 `git add`、`git commit` 或批量格式化，避免把既有改动误收进提交。

**执行状态：** 代码与文档改动已完成；非数据库单测、工作台测试、Ruff、mypy、前端构建与差异空白检查均通过。API/商品组合根集成测试尚未运行，当前 Docker Desktop/PostgreSQL 不可用，恢复数据库后需补跑相关集成测试及完整后端测试套件。

---

## 文件结构

- Create: `runtime/workspace/agent_binding.py` — Workspace 唯一有效 AgentVersion 解析服务。
- Create: `runtime/capabilities/skill_activation.py` — `load_skill` 定义、Skill 目录渲染、激活与恢复查询。
- Create: `composition/__init__.py` — 产品组合包入口。
- Move: `adapters/commerce/product_runtime.py` → `composition/product_operations.py` — 商品运行组合根。
- Delete: `apps/api/routes/agents.py` — 当前产品不再暴露全局 Agent 目录。
- Modify: `apps/api/schemas/sessions.py`、`apps/api/routes/sessions.py`、`runtime/workspace/session_commands.py` — Session 服务端解析 Agent。
- Modify: `apps/api/services/workspace_overview.py` — 复用同一 Agent 绑定解析服务。
- Modify: `apps/api/main.py` — 移除 Agent 目录路由。
- Modify: `apps/workbench/src/app/TaskComposer.tsx`、相关测试与 E2E fixture — 不再提交 `agent_id`。
- Modify: `runtime/orchestration/context.py`、`runtime/orchestration/agent_loop.py` — Skill 目录常驻、激活正文注入。
- Modify: `runtime/capabilities/registry.py`、`agents/product_operations/agent.py`、`agents/product_operations/skill.py` — Skill 描述与 `load_skill` 能力声明。
- Modify: `apps/worker/assembly.py`、`eval/*.py`、商品测试夹具 — 使用新组合根路径。
- Modify: `README.md`、架构与需求文档 — 校准单 Agent 与 Skill 语义。

### Task 1: 由服务端解析 Workspace 唯一 Agent

**Files:**
- Create: `runtime/workspace/agent_binding.py`
- Modify: `runtime/workspace/session_commands.py`
- Modify: `apps/api/schemas/sessions.py`
- Modify: `apps/api/routes/sessions.py`
- Modify: `apps/api/services/workspace_overview.py`
- Test: `tests/apps/api/test_run_commands.py`
- Test: `tests/apps/api/test_workspace_overview.py`

- [ ] **Step 1: 写 Session 请求不接受 `agent_id` 的失败测试**

```python
response = await owner_client.post(
    "/api/sessions",
    json={"workspace_id": str(workspace.id), "title": "商品维护"},
)
assert response.status_code == 201
assert response.json()["agent_id"] == str(workspace.agent_id)

ignored = await owner_client.post(
    "/api/sessions",
    json={
        "workspace_id": str(workspace.id),
        "agent_id": str(uuid.uuid4()),
    },
)
assert ignored.status_code == 422
```

- [ ] **Step 2: 运行单测并确认因当前 Schema 仍要求 `agent_id` 而失败**

Run: `uv run pytest tests/apps/api/test_run_commands.py -k "create_session" -q`

Expected: 新测试返回 `422`，而不是 `201`。

- [ ] **Step 3: 新增共享解析服务**

```python
@dataclass(frozen=True)
class ResolvedWorkspaceAgent:
    agent_id: UUID
    agent_version_id: UUID
    stable_key: str
    name: str
    version: int


class WorkspaceAgentResolver:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, workspace_id: UUID) -> ResolvedWorkspaceAgent:
        rows = (
            await self._session.execute(
                select(WorkspaceAgentBinding, AgentVersion, Agent)
                .join(
                    AgentVersion,
                    AgentVersion.id == WorkspaceAgentBinding.agent_version_id,
                )
                .join(Agent, Agent.id == AgentVersion.agent_id)
                .where(WorkspaceAgentBinding.workspace_id == workspace_id)
                .where(WorkspaceAgentBinding.enabled.is_(True))
                .where(WorkspaceAgentBinding.effective_at <= func.now())
                .where(AgentVersion.status == ConfigVersionStatus.PUBLISHED)
                .order_by(WorkspaceAgentBinding.effective_at, WorkspaceAgentBinding.id)
            )
        ).all()
        if not rows:
            raise WorkspaceAgentNotConfigured(workspace_id)
        if len(rows) > 1:
            raise WorkspaceAgentConfigurationInvalid(workspace_id)
        _, version, agent = rows[0]
        return ResolvedWorkspaceAgent(
            agent_id=agent.id,
            agent_version_id=version.id,
            stable_key=agent.stable_key,
            name=agent.name,
            version=version.version,
        )
```

实现时使用现有 `WorkspaceAgentBinding → AgentVersion → Agent` 查询，并把两个错误映射为现有命令/API 错误结构；不要把“取第一项”作为降级策略。

- [ ] **Step 4: 修改 Session 创建接口**

```python
class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    title: str | None = Field(default=None, max_length=255)


async def create_session(self, ctx: ActorContext, *, title: str | None) -> Session:
    selected = await WorkspaceAgentResolver(self._session).resolve(ctx.workspace_id)
    return await self._sessions.create(
        workspace_id=ctx.workspace_id,
        agent_id=selected.agent_id,
        agent_version_id=selected.agent_version_id,
        created_by_actor_id=self._actor_id,
        created_by_membership_id=ctx.membership_id,
        title=title,
    )
```

`WorkspaceOverviewService` 使用同一个 Resolver，不再复制绑定查询和错误判断。

- [ ] **Step 5: 运行 API 测试**

Run: `uv run pytest tests/apps/api/test_run_commands.py tests/apps/api/test_workspace_overview.py -q`

Expected: PostgreSQL 可用时全部通过；未启动时只报告连接基础设施错误，不修改测试绕过数据库。

- [ ] **Step 6: 复核本任务差异**

```bash
git diff -- runtime/workspace/agent_binding.py runtime/workspace/session_commands.py apps/api/schemas/sessions.py apps/api/routes/sessions.py apps/api/services/workspace_overview.py tests/apps/api/test_run_commands.py tests/apps/api/test_workspace_overview.py
```

### Task 2: 清理工作台的 Agent 选择语义

**Files:**
- Delete: `apps/api/routes/agents.py`
- Modify: `apps/api/main.py`
- Modify: `apps/workbench/src/app/TaskComposer.tsx`
- Modify: `apps/workbench/src/app/TaskComposer.test.tsx`
- Modify: `apps/workbench/e2e/fixtures/runtime.ts`

- [ ] **Step 1: 修改前端测试，断言 Session 请求不包含 `agent_id`**

```typescript
expect(fetchMock).toHaveBeenCalledWith(
  "/api/sessions",
  expect.objectContaining({
    body: JSON.stringify({ workspace_id: "ws-brand-1" }),
  }),
);
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `npm --prefix apps/workbench run test -- --run src/app/TaskComposer.test.tsx`

Expected: 请求体仍含 `agent_id`，断言失败。

- [ ] **Step 3: 保留 Agent 配置状态但移除 Agent ID 提交**

`TaskComposer` 可以继续接收 `agent` 以显示“当前 Workspace 未配置商品运营 Agent”，但请求体只发送：

```typescript
const session = await postJSON<SessionCreated>("/sessions", {
  workspace_id: workspaceId,
});
```

移除 `apps/api/main.py` 对 `agents_router` 的注册并删除 `apps/api/routes/agents.py`；删除 E2E fixture 中 `/api/agents` mock。

- [ ] **Step 4: 运行前端单测、Lint 和构建**

Run: `npm --prefix apps/workbench run test -- --run`

Run: `npm --prefix apps/workbench run lint`

Run: `npm --prefix apps/workbench run build`

Expected: 三条命令退出码均为 0。

- [ ] **Step 5: 复核本任务差异**

```bash
git diff -- apps/api/main.py apps/api/routes/agents.py apps/workbench/src/app/TaskComposer.tsx apps/workbench/src/app/TaskComposer.test.tsx apps/workbench/e2e/fixtures/runtime.ts
```

### Task 3: 将商品组合根移出 Adapter

**Files:**
- Create: `composition/__init__.py`
- Move: `adapters/commerce/product_runtime.py` → `composition/product_operations.py`
- Modify: `apps/worker/assembly.py`
- Modify: `eval/fixtures.py`
- Modify: `eval/runner.py`
- Modify: `eval/recovery_suite.py`
- Modify: `eval/product_suite.py`
- Modify: `tests/architecture/test_dependency_boundaries.py`
- Modify: 引用旧路径的注释和文档

- [ ] **Step 1: 添加组合根边界失败测试**

```python
def test_adapters_do_not_own_product_runtime_composition() -> None:
    assert not Path("adapters/commerce/product_runtime.py").exists()
    assert Path("composition/product_operations.py").exists()


def test_runtime_does_not_import_composition() -> None:
    violations = _imports_from("runtime", "composition.")
    assert violations == []
```

- [ ] **Step 2: 运行测试并确认旧文件仍存在导致失败**

Run: `uv run pytest tests/architecture/test_dependency_boundaries.py -q`

- [ ] **Step 3: 移动文件并更新导入**

使用 `git mv adapters/commerce/product_runtime.py composition/product_operations.py`，新增 `composition/__init__.py`，将 Eval 直接导入改为：

```python
from composition.product_operations import (
    ProductApplication,
    build_product_application,
)
```

`apps/worker/assembly.py` 仅做兼容导出，不再声称组合根属于 Adapter。

- [ ] **Step 4: 扫描旧路径并运行架构测试**

Run: `rg -n "adapters\.commerce\.product_runtime" . -g "*.py" -g "*.md"`

Expected: 无有效旧导入或错误文档描述。

Run: `uv run pytest tests/architecture tests/agents/product_operations/test_agent_assembly.py -q`

- [ ] **Step 5: 复核本任务差异**

```bash
git diff -- composition adapters/commerce/product_runtime.py apps/worker/assembly.py eval tests/architecture docs README.md
```

### Task 4: Context 支持 Skill 目录和已激活正文

**Files:**
- Modify: `runtime/capabilities/registry.py`
- Modify: `runtime/orchestration/context.py`
- Modify: `tests/runtime/orchestration/conftest.py`
- Modify: `tests/runtime/orchestration/test_context_budget.py`

- [ ] **Step 1: 写 Skill 目录逐步加载的失败测试**

```python
def test_context_exposes_skill_catalog_without_full_instructions(assembler):
    request = assembler.build(
        ConversationView(
            active_skill_instructions=(),
        )
    )
    assert request.contains("price_update: 修改商品价格")
    assert not request.contains("完整改价 SOP 正文")


def test_context_protects_only_activated_skill_instructions(assembler):
    request = assembler.build(
        ConversationView(
            active_skill_instructions=(
                ActiveSkillInstruction(
                    stable_key="price_update",
                    version=1,
                    content="完整改价 SOP 正文",
                ),
            )
        )
    )
    assert request.contains("完整改价 SOP 正文")
```

- [ ] **Step 2: 运行测试并确认类型或参数缺失导致 RED**

Run: `uv run pytest tests/runtime/orchestration/test_context_budget.py -k "skill" -q`

- [ ] **Step 3: 扩展 SkillDefinition 与 ContextAssembler**

```python
class SkillDefinition(BaseModel):
    name: str | None = None
    description: str = ""
    required_tool_keys: frozenset[str] = frozenset()
    required_source_keys: frozenset[str] = frozenset()
    prompt_fragments: dict[str, Any] = {}


@dataclass(frozen=True)
class SkillCatalogEntry:
    stable_key: str
    name: str
    description: str
    version: int


@dataclass(frozen=True)
class ActiveSkillInstruction:
    stable_key: str
    version: int
    content: str
```

`ContextAssembler` 新增 `skill_catalog: tuple[SkillCatalogEntry, ...]`；目录作为受保护的 Agent 指令常驻。`ConversationView.active_skill_instructions` 中的正文同样使用 `ContextItemKind.AGENT_INSTRUCTIONS`，但每个 Skill 使用独立 key，不能拼成无法追踪的大字符串。

- [ ] **Step 4: 运行 Context 全量测试**

Run: `uv run pytest tests/runtime/orchestration/test_context_budget.py tests/runtime/orchestration/test_agent_loop.py -q`

- [ ] **Step 5: 复核本任务差异**

```bash
git diff -- runtime/capabilities/registry.py runtime/orchestration/context.py tests/runtime/orchestration/conftest.py tests/runtime/orchestration/test_context_budget.py
```

### Task 5: 实现持久化 `load_skill`

**Files:**
- Create: `runtime/capabilities/skill_activation.py`
- Modify: `runtime/orchestration/agent_loop.py`
- Test: `tests/runtime/capabilities/test_skill_activation.py`
- Test: `tests/runtime/orchestration/test_agent_loop.py`

- [ ] **Step 1: 写激活范围、幂等和恢复失败测试**

```python
async def test_activate_skill_must_belong_to_run_snapshot(
    db_session: AsyncSession,
    skill_activation_seed: SkillActivationSeed,
):
    service = SkillActivationService(
        db_session,
        workspace_id=skill_activation_seed.workspace.id,
    )
    with pytest.raises(SkillNotAvailable):
        await service.activate(skill_activation_seed.run.id, "not-pinned")


async def test_activate_skill_is_idempotent_and_recoverable(
    db_session: AsyncSession,
    skill_activation_seed: SkillActivationSeed,
):
    service = SkillActivationService(
        db_session,
        workspace_id=skill_activation_seed.workspace.id,
    )
    first = await service.activate(
        skill_activation_seed.run.id,
        "product_maintenance",
    )
    second = await service.activate(
        skill_activation_seed.run.id,
        "product_maintenance",
    )
    assert first.skill_version_id == second.skill_version_id
    assert len(await service.active_for_run(skill_activation_seed.run.id)) == 1
```

在同一测试文件定义 `SkillActivationSeed` 与 `skill_activation_seed`：创建一个
Workspace、Session、Run、`product_maintenance` SkillVersion 和对应
`RunSkillVersion`，并把 `session`、`workspace`、`run` 作为具名字段返回；测试
中的 `service`、`workspace`、`run` 均从该 seed 读取，避免依赖隐式全局状态。

- [ ] **Step 2: 运行测试并确认模块不存在导致 RED**

Run: `uv run pytest tests/runtime/capabilities/test_skill_activation.py -q`

- [ ] **Step 3: 实现 Skill 激活服务和 Runtime Tool 定义**

```python
LOAD_SKILL_KEY = "load_skill"
SKILL_ACTIVATED = "SKILL_ACTIVATED"

_LOAD_SKILL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"skill_key": {"type": "string"}},
    "required": ["skill_key"],
    "additionalProperties": False,
}
_LOAD_SKILL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_key": {"type": "string"},
        "version": {"type": "integer"},
        "instructions": {"type": "string"},
        "already_active": {"type": "boolean"},
    },
    "required": ["skill_key", "version", "instructions", "already_active"],
    "additionalProperties": False,
}

LOAD_SKILL_DEFINITION = ToolDefinition(
    stable_key=LOAD_SKILL_KEY,
    version=1,
    description="加载当前 Agent 已声明的 Skill 完整说明。",
    input_json_schema=_LOAD_SKILL_INPUT_SCHEMA,
    output_json_schema=_LOAD_SKILL_OUTPUT_SCHEMA,
    effect=ToolEffect.READ,
    required_permissions=frozenset(),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        _LOAD_SKILL_INPUT_SCHEMA,
        _LOAD_SKILL_OUTPUT_SCHEMA,
    ),
)
```

`SkillActivationService.activate()` 必须通过 `RunSkillVersion → SkillVersion → Skill` 校验 Run 快照；首次激活写 `SKILL_ACTIVATED` AgentEvent，重复激活读取已有事件。`active_for_run()` 从事件恢复 SkillVersion 和正文，不依赖 Worker 内存。

`load_skill` 仍作为 Run 快照中的 Runtime 内置只读 Tool 暴露。ToolRegistry 为它登记显式的 Runtime 内置绑定标记，以满足启动期完整性校验；`AgentLoop` 按稳定键在业务 `ReadToolHandler` 之前接管执行，不让商品 Adapter 承担 Skill 激活职责。

- [ ] **Step 4: 在 AgentLoop 中处理 Runtime 内置 Tool**

`_execute_read()` 在调用业务 ReadToolHandler 前识别 `LOAD_SKILL_KEY`，使用当前数据库 Session 激活 Skill，生成标准 `ToolExecutionReceipt`，再沿用现有 PostToolUse、ToolCall 完成和 ToolResult 回填流程。

`_conversation_view()` 调用 `active_for_run(run.id)`，把完整正文填入 `ConversationView.active_skill_instructions`。这样重启后只需事件重放即可恢复。

- [ ] **Step 5: 运行能力与 Loop 测试**

Run: `uv run pytest tests/runtime/capabilities/test_skill_activation.py tests/runtime/orchestration/test_agent_loop.py -q`

- [ ] **Step 6: 复核本任务差异**

```bash
git diff -- runtime/capabilities/skill_activation.py runtime/orchestration/agent_loop.py tests/runtime/capabilities/test_skill_activation.py tests/runtime/orchestration/test_agent_loop.py
```

### Task 6: 商品 Agent 接入 Skill 目录

**Files:**
- Modify: `agents/product_operations/skill.py`
- Modify: `agents/product_operations/agent.py`
- Modify: `composition/product_operations.py`
- Modify: `tests/agents/product_operations/test_agent_assembly.py`
- Modify: `tests/eval/test_runner_isolation.py`

- [ ] **Step 1: 写商品 Agent 的目录与加载行为失败测试**

```python
assert PRODUCT_MAINTENANCE_SKILL.description
assert LOAD_SKILL_KEY in PRODUCT_AGENT_DEFINITION.required_tool_keys
backend.set_rounds([[BackendCompleted(finish_reason="stop")]])
await product_runtime.start("查询 SKU-001")
request_text = "\n".join(
    message.content for message in backend.requests[0].messages
)
assert SKILL_KEY in request_text
assert PRODUCT_MAINTENANCE_SKILL.description in request_text
assert PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS not in request_text
```

给测试夹具 `ScriptedAgentBackend` 增加 `requests: list[BackendRequest]`，并在
`stream()` 开始时追加请求；不要增加仅供测试使用的生产接口。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/agents/product_operations/test_agent_assembly.py -q`

- [ ] **Step 3: 注册 `load_skill` 并改为目录常驻**

- 为 `PRODUCT_MAINTENANCE_SKILL` 增加清晰 description；
- AgentDefinition 的 Tool 集合加入 `load_skill`；
- 组合根将 `LOAD_SKILL_DEFINITION` 注册、落库并绑定；
- `ContextAssembler.agent_instructions` 只传 `PRODUCT_AGENT_PROMPT`；
- `skill_catalog` 从已解析 SkillVersionRecord 生成；
- 不再把完整 Skill 正文静态拼入 Agent Prompt。

- [ ] **Step 4: 运行商品 Agent 与 Eval 确定性测试**

Run: `uv run pytest tests/agents/product_operations tests/eval/test_runner_isolation.py tests/eval/test_scorers.py -q`

- [ ] **Step 5: 复核本任务差异**

```bash
git diff -- agents/product_operations composition/product_operations.py tests/agents/product_operations/test_agent_assembly.py tests/eval/test_runner_isolation.py
```

### Task 7: 文档校准与最终验证

**Files:**
- Modify: `README.md`
- Modify: `docs/需求/Agent工作台与Runtime需求文档.md`
- Modify: `docs/架构/Runtime核心接口与Harness设计.md`
- Modify: `docs/架构/商品运营参考Agent设计.md`
- Modify: 受旧 API/路径影响的其他文档

- [ ] **Step 1: 更新文档事实**

文档必须明确：

- 当前产品只有商品运营 Agent；
- Workspace 绑定唯一 AgentVersion，用户不手动选择 Agent；
- 同一模型依据 Skill 名称和描述选择并调用 `load_skill`；
- 没有独立 Agent Router 或 Skill Router；
- Skill 是软流程指导，权限和业务安全仍由 Runtime/业务服务硬校验；
- 组合根位于 `composition/`。

- [ ] **Step 2: 运行静态检查**

Run: `uv run ruff check runtime agents adapters composition apps eval tests`

Run: `uv run mypy runtime agents adapters composition apps eval`

Run: `git diff --check`

- [ ] **Step 3: 运行后端、Eval 与前端验证**

Run: `uv run pytest -q`

Run: `npm --prefix apps/workbench run test -- --run`

Run: `npm --prefix apps/workbench run lint`

Run: `npm --prefix apps/workbench run build`

如果 PostgreSQL 未启动，先报告基础设施阻塞；不得把连接失败描述为代码测试失败或通过。

- [ ] **Step 4: 对照规格验收**

逐项核对 `docs/superpowers/specs/2026-10-06-single-agent-progressive-skills-design.md` 第 9 节，确认没有实现 Agent 自动路由、Skill Router、Skill 级 Tool 裁剪或第二个业务 Agent。

- [ ] **Step 5: 复核文档与最终收口差异**

```bash
git diff -- README.md docs/需求/Agent工作台与Runtime需求文档.md docs/架构/Runtime核心接口与Harness设计.md docs/架构/商品运营参考Agent设计.md
```
