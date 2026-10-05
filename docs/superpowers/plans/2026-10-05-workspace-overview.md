# Workspace 工作台收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Workspace 工作台展示当前成员有权访问的真实配置，并保证会话执行使用该 Workspace 启用的确切 Agent 版本。

**Architecture:** 在 apps/api 增加 Workspace overview 聚合读取服务，由它读取 Runtime 配置和商品运营店铺；Runtime 本身保持业务无关。API 与前端统一执行模式/成员角色，Session 固定已启用的 AgentVersion，页面与任务创建只依赖 Workspace overview。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy Async、PostgreSQL、Alembic、React、TypeScript、Vitest、Playwright、pytest。

---

## 当前进度与边界

正式设计见 docs/superpowers/specs/2026-10-05-workspace-overview-design.md。目前已有未提交实现：

- apps/api/services/workspace_overview.py：overview 聚合服务雏形；
- apps/api/routes/workspaces.py：overview 路由；
- apps/api/service_errors.py：应用服务错误翻译扩展；
- tests/apps/api/test_workspace_overview.py：overview 集成测试雏形。

这些文件属于 Task 1 的进行中工作，不要覆盖或重建。初始验证发现 overview 测试夹具存在外键清理与 UUID/string 类型问题，Ruff 发现 3 个错误；Task 1 修复后 overview 与 Workspace 权限测试共 20 项通过，Ruff 和 mypy 通过。

本计划不包含业务规则接入、MCP/WeKnora 能力接线和模型 Eval；它们是下一阶段独立任务。

## 文件结构与职责

| 文件 | 职责 |
|---|---|
| apps/api/services/workspace_overview.py | 聚合 Workspace 详情、当前成员授权、店铺、Agent 与配置摘要 |
| apps/api/services/errors.py | 定义 API 应用服务共享错误基类，供路由统一翻译 |
| apps/api/routes/workspaces.py | 暴露 overview 读取端点，不在只读路由提交事务 |
| apps/api/service_errors.py | 统一翻译 Runtime 与 API 应用服务错误 |
| runtime/persistence/models/interaction.py | Session 持久化精确 AgentVersion 引用 |
| runtime/persistence/repositories/sessions.py | 创建 Session 时保存 AgentVersion 引用 |
| runtime/workspace/session_commands.py | 校验 Workspace Agent 绑定并用 Session 固定版本创建 Run |
| migrations/versions/0003_session_agent_version.py | 给现有 Session 增加 AgentVersion 引用及约束 |
| apps/api/schemas/sessions.py、apps/api/routes/sessions.py | 创建 Session 输入/输出保持兼容，服务端从 Workspace 绑定解析版本 |
| apps/workbench/src/features/workspaces/types.ts | 前端 API 类型与后端枚举一致 |
| apps/workbench/src/features/workspaces/WorkspaceContainer.tsx | 加载 overview、保存模式并重新读取权威状态 |
| apps/workbench/src/features/workspaces/WorkspacePage.tsx | 展示真实配置及按真实成员角色控制配置入口 |
| apps/workbench/src/app/App.tsx、WorkspaceView.tsx | 移除写死角色，向页面传递 overview 数据 |
| apps/workbench/src/app/TaskComposer.tsx | 使用 Workspace 已启用的 Agent ID 创建会话 |
| apps/workbench/src/test/fixtures.ts | 用真实后端响应形状构造前端测试数据 |
| tests/apps/api/test_workspace_overview.py | overview、权限过滤、跨 Workspace 隔离和错误语义 |
| tests/apps/api/test_run_commands.py | Session Agent 绑定、版本固定和越 Workspace 拒绝 |
| apps/workbench/src/features/workspaces/WorkspacePage.test.tsx | overview 页面、执行模式、角色和 Agent 缺失状态 |
| apps/workbench/src/app/TaskComposer.test.tsx | 任务创建使用 overview Agent 且不调用全局 Agent 列表 |
| apps/workbench/e2e/fixtures/runtime.ts 及 E2E spec | 多 Workspace UI 隔离和真实后端任务创建 |

## 实施任务

### Task 1：修复现有 overview 集成测试与 API 聚合雏形

**Files:**
- Modify: tests/apps/api/test_workspace_overview.py
- Modify: apps/api/services/workspace_overview.py
- Modify: apps/api/routes/workspaces.py
- Modify: apps/api/service_errors.py
- Create: apps/api/services/errors.py
- Test: tests/apps/api/test_workspace_overview.py

- [x] **Step 1：修复测试夹具中的类型与删除顺序**

在 test_workspace_overview.py 中将 MembershipResourceScope.resource_id 写入值统一为 str(store_id)。SeededWorkspace.agent_version 改为 agent_versions: tuple[AgentVersion, ...]，每次创建 AgentVersion 都加入集合。清理时先删除所有 WorkspaceAgentBinding，再按 AgentVersion ID 集合删除版本，之后删除 Agent；先删除所有测试 Workspace 的 memberships，再去重删除被这些 Workspace 共用的 User。清理 Listing、Product、Store 时保持 Listing → Product → Store 顺序。删除未使用的 sqlalchemy.select，未使用解包变量改为 _client。

- [x] **Step 2：重新运行 overview 测试确认夹具测试通过**

Run: uv run pytest tests/apps/api/test_workspace_overview.py -q --tb=short

Expected: 所有 overview 用例通过，teardown 不出现外键异常或 SQLAlchemy 连接泄漏警告。若仍失败，按具体失败继续先修夹具/测试输入，不能放宽断言。

- [x] **Step 3：新增 Agent 绑定生效时间回归用例**

在 test_workspace_overview.py 增加一个未来 effective_at 的已启用且已发布绑定，断言 overview 返回 agent: null。多版本测试也应覆盖一条当前有效绑定与一条未来绑定并存时只返回当前有效版本。

- [x] **Step 4：让 overview 仅选择当前有效绑定**

在 _enabled_agent 查询中增加 WorkspaceAgentBinding.effective_at <= now_utc 条件，使用 timezone-aware UTC 时间；继续限定 enabled IS TRUE 和 AgentVersion.status == PUBLISHED。0 条返回 None，1 条返回摘要，多于 1 条抛 AgentConfigurationInvalid。

- [x] **Step 5：移除 overview GET 的 commit**

get_workspace_overview 只读取数据，删除 await session.commit()。保留统一错误翻译。用现有请求级 Session 生命周期负责关闭只读事务。

- [x] **Step 6：抽取 API 应用服务错误基类**

在 apps/api/services/errors.py 定义 ApplicationServiceError(Exception)，具有 status_code 和 code。WorkspaceOverviewError 继承此基类；apps/api/service_errors.py 只依赖该基类和 WorkspaceServiceError。overview 路由及其他 Workspace 配置路由的 Runtime 错误翻译行为保持不变。

- [x] **Step 7：跑后端静态检查和相关 API 回归**

Run: uv run ruff check apps/api/routes/workspaces.py apps/api/service_errors.py apps/api/services tests/apps/api/test_workspace_overview.py

Run: uv run mypy apps/api/services apps/api/routes/workspaces.py apps/api/service_errors.py

Run: uv run pytest tests/apps/api/test_workspace_overview.py tests/apps/api/test_workspace_permissions.py -q

Expected: Ruff 无错误，mypy 无错误，两个 API 测试文件全绿。

- [x] **Step 8：提交 Task 1**

~~~powershell
git add apps/api/routes/workspaces.py apps/api/service_errors.py apps/api/services tests/apps/api/test_workspace_overview.py
git commit -m "feat: add workspace overview endpoint"
~~~

### Task 2：将 Session 固定到 Workspace 启用的 AgentVersion

**Files:**
- Modify: runtime/persistence/models/interaction.py
- Modify: runtime/persistence/repositories/sessions.py
- Modify: runtime/workspace/session_commands.py
- Modify: apps/api/routes/sessions.py
- Modify: apps/api/schemas/sessions.py
- Create: migrations/versions/0003_session_agent_version.py
- Test: tests/apps/api/test_run_commands.py
- Test: tests/apps/api/test_workspace_overview.py

- [ ] **Step 1：先写 Session 创建拒绝未绑定 Agent 的 API 测试**

通过现有 API test fixture 创建两个 Agent，其中仅一个 AgentVersion 在目标 Workspace 启用。POST /api/sessions 使用未绑定 Agent ID 时，断言返回 409 且稳定错误码为 AGENT_NOT_ENABLED_FOR_WORKSPACE；使用绑定 Agent ID 时断言 201。

- [ ] **Step 2：运行单个新测试确认因当前缺少绑定校验失败**

Run: uv run pytest tests/apps/api/test_run_commands.py -k workspace_agent_binding -q --tb=short

Expected: 未绑定 Agent 用例当前失败，指出 API 接受了它；失败必须来自断言而非测试 fixture 崩溃。

- [ ] **Step 3：给 Session 增加必需的 agent_version_id 外键**

在 Session 模型新增 agent_version_id: Mapped[uuid.UUID]，外键到 agent_versions.id，非空。创建迁移 0003_session_agent_version.py：升级时优先按该 Session 最近一条 Run 的 agent_version_id 回填；没有历史 Run 时，按该 Agent 最高版本的 PUBLISHED AgentVersion 回填；无已发布版本的历史 Session 必须使迁移失败并给出明确错误，不得静默选 DRAFT；回填后设为 NOT NULL 并添加命名外键。降级只删除新列和外键。

- [ ] **Step 4：在 SessionCommandService 中根据 Workspace 绑定解析版本**

新增内部解析方法，查询 workspace_id + agent_id + enabled + effective_at <= now_utc 的绑定，再要求 AgentVersion 为 PUBLISHED。0 条抛 SessionCommandError(409, AGENT_NOT_ENABLED_FOR_WORKSPACE, ...)；多条抛配置错误 409；唯一结果同时校验 AgentVersion.agent_id == agent_id。调用 SessionRepository.create 时把解析出的 version ID 与 agent ID 一并传入。

- [ ] **Step 5：Repository 持久化精确版本，Run 创建复用该版本**

SessionRepository.create 接受必需参数 agent_version_id 并写入 Session。append_message 不再调用 _latest_agent_version_id(session_row.agent_id)，直接使用 session_row.agent_version_id 创建 Run。删除因此无用的 _latest_agent_version_id 方法；更新 Session schema/fixture 中需要暴露的新字段。

- [ ] **Step 6：补充版本固定与越 Workspace 回归测试**

测试同一 Agent 有已启用版本 v1，之后目录中出现更高版本 v2 但未启用时，新 Run 仍绑定 v1；测试 Workspace B 的绑定不能授权 Workspace A 创建 Session；测试未来生效和已禁用绑定均被拒绝。

- [ ] **Step 7：运行迁移、Session API 测试和类型检查**

Run: uv run alembic upgrade head

Run: uv run pytest tests/apps/api/test_run_commands.py tests/apps/api/test_workspace_overview.py -q

Run: uv run ruff check runtime/persistence/models/interaction.py runtime/persistence/repositories/sessions.py runtime/workspace/session_commands.py apps/api/routes/sessions.py apps/api/schemas/sessions.py migrations/versions/0003_session_agent_version.py tests/apps/api

Run: uv run mypy runtime apps/api

Expected: 迁移升级成功；绑定/版本用例通过；Ruff、mypy 无错误。

- [ ] **Step 8：提交 Task 2**

~~~powershell
git add runtime apps/api/routes/sessions.py apps/api/schemas/sessions.py migrations/versions/0003_session_agent_version.py tests/apps/api/test_run_commands.py tests/apps/api/test_workspace_overview.py
git commit -m "feat: pin workspace agent version to session"
~~~

### Task 3：让 Workspace overview 成为前端唯一读取模型

**Files:**
- Modify: apps/workbench/src/features/workspaces/types.ts
- Modify: apps/workbench/src/features/workspaces/WorkspaceContainer.tsx
- Modify: apps/workbench/src/features/workspaces/WorkspacePage.tsx
- Modify: apps/workbench/src/app/App.tsx
- Modify: apps/workbench/src/app/WorkspaceView.tsx
- Modify: apps/workbench/src/test/fixtures.ts
- Test: apps/workbench/src/features/workspaces/WorkspacePage.test.tsx

- [ ] **Step 1：先调整前端测试 fixture 为 overview 真实响应形状**

在 fixtures.ts 定义 workspaceOverview，字段包括 workspace.execution_mode、current_membership.role、stores、agent、model_connections、skills、sources、business_policies。枚举使用 READ_ONLY | CONFIRM_WRITES | AUTO_EXECUTE 与 WORKSPACE_OWNER | OPERATOR。

- [ ] **Step 2：先写三种模式与角色来源测试**

在 WorkspacePage.test.tsx 断言：Owner 能修改三种模式；Operator 不能编辑；模式文本显示 READ_ONLY、CONFIRM_WRITES、AUTO_EXECUTE；WorkspaceContainer 请求 /workspaces/{id}/overview 并渲染 current_membership.role。

- [ ] **Step 3：运行前端定向测试确认旧类型/请求形状不符合期望**

Run: npm --prefix apps/workbench run test -- --run src/features/workspaces/WorkspacePage.test.tsx

Expected: 新增断言在现状下失败，证明前端仍使用基础详情、AWAIT_CONFIRMATION 或调用方硬编码角色。

- [ ] **Step 4：更新前端 API 类型与页面模型**

用 ExecutionMode = "READ_ONLY" | "CONFIRM_WRITES" | "AUTO_EXECUTE"、WorkspaceRole = "WORKSPACE_OWNER" | "OPERATOR"；定义 overview 类型。删除 AWAIT_CONFIRMATION、前端别名 OWNER、强制类型转换和默认补值。WorkspaceContainer 对缺少必需字段返回明确错误，不把不完整响应转换为空页面。

- [ ] **Step 5：更新 Container、Page 和 App 的角色数据流**

Container 拉取 /workspaces/{workspaceId}/overview，WorkspacePage 接收 overview，Owner 判断来自 overview.current_membership.role。移除 WorkspaceView.currentRole 属性和 App 传入的 "OPERATOR" 常量。保存执行模式仍调用现有 PATCH 接口，成功后重新拉 overview。

- [ ] **Step 6：运行前端定向测试、lint 与 build**

Run: npm --prefix apps/workbench run test -- --run src/features/workspaces/WorkspacePage.test.tsx

Run: npm --prefix apps/workbench run lint

Run: npm --prefix apps/workbench run build

Expected: 新增页面测试通过，lint 和 TypeScript build 成功。

- [ ] **Step 7：提交 Task 3**

~~~powershell
git add apps/workbench/src/features/workspaces apps/workbench/src/app/App.tsx apps/workbench/src/app/WorkspaceView.tsx apps/workbench/src/test/fixtures.ts
git commit -m "feat: render workspace overview in workbench"
~~~

### Task 4：按 Workspace 启用 Agent 创建任务

**Files:**
- Modify: apps/workbench/src/app/TaskComposer.tsx
- Modify: apps/workbench/src/app/WorkspaceView.tsx
- Modify: apps/workbench/src/app/TaskComposer.test.tsx
- Modify: apps/workbench/src/test/fixtures.ts
- Test: tests/apps/api/test_run_commands.py

- [ ] **Step 1：添加 TaskComposer 的缺少 Agent 与请求断言测试**

注入 overview 的 Agent 摘要。Agent 为 null 时断言发送按钮禁用并显示明确提示；Agent 存在时断言 POST /sessions 的 agent_id 等于 overview 中的 ID，且 fetch 未调用 /agents。

- [ ] **Step 2：运行定向前端测试确认它发现全局目录调用**

Run: npm --prefix apps/workbench run test -- --run src/app/TaskComposer.test.tsx

Expected: 当前实现因为请求 /agents 而失败。

- [ ] **Step 3：将 overview Agent 显式传入 TaskComposer**

给 TaskComposerProps 增加 agent: OverviewAgentView | null。删去 /agents 请求与 agents[0]；Agent 为 null 时禁用输入/发送并显示“当前 Workspace 未启用商品运营 Agent”；创建 Session 时发送 Workspace ID 和该 Agent ID。

- [ ] **Step 4：验证成功/缺失状态和 Session 绑定服务端拒绝**

Run: npm --prefix apps/workbench run test -- --run src/app/TaskComposer.test.tsx src/features/workspaces/WorkspacePage.test.tsx

Run: uv run pytest tests/apps/api/test_run_commands.py -k workspace_agent_binding -q

Expected: 前端只用 Workspace Agent；后端阻止缺失/未启用绑定。

- [ ] **Step 5：提交 Task 4**

~~~powershell
git add apps/workbench/src/app/TaskComposer.tsx apps/workbench/src/app/WorkspaceView.tsx apps/workbench/src/app/TaskComposer.test.tsx apps/workbench/src/test/fixtures.ts
git commit -m "feat: create tasks with workspace agent"
~~~

### Task 5：补齐双 Workspace API 与工作台端到端验收

**Files:**
- Modify: tests/apps/api/test_workspace_overview.py
- Modify: apps/workbench/e2e/fixtures/runtime.ts
- Modify: apps/workbench/e2e 中现有工作台 spec
- Test: tests/apps/api/test_workspace_overview.py
- Test: apps/workbench/e2e

- [ ] **Step 1：扩展 overview 两品牌隔离断言**

为 Workspace A/B 准备不同成员、店铺、Agent、模型连接与 Skill/Source 摘要；使用同一 Owner 的两个 Workspace 成员身份分别请求 overview。断言每个响应只包含对应 Workspace 的对象和成员授权。Operator 的 SELECTED 只暴露指定店铺。

- [ ] **Step 2：扩展 API 测试覆盖所有边界**

验证非成员 403、无绑定 agent: null、多有效绑定 409、未来绑定不生效、Secret 仅为掩码、错误 Workspace 不能通过引用另一 Workspace 的 AgentVersion 创建 Session。

- [ ] **Step 3：更新 Playwright fixture 和页面流程**

E2E seed 建立两个 Workspace、同一用户的双成员身份、不同店铺与明确的 WorkspaceAgentBinding；Owner 页面切换 Workspace 后确认品牌名/店铺切换；Operator 页面确认无执行模式编辑入口；创建任务后 Session 与当前 Workspace 启用的 AgentVersion 一致。

- [ ] **Step 4：运行全套相关测试和发布检查**

Run: uv run pytest tests/apps/api/test_workspace_overview.py tests/apps/api/test_workspace_permissions.py tests/apps/api/test_run_commands.py -q

Run: npm --prefix apps/workbench run lint

Run: npm --prefix apps/workbench run test -- --run

Run: npm --prefix apps/workbench run build

Run: npm --prefix apps/workbench run test:e2e

Run: uv run ruff check runtime apps tests

Run: uv run mypy runtime agents adapters apps eval

Run: git diff --check

Expected: 所有相关 API、前端、E2E、静态检查与格式检查通过；Runtime 架构边界测试通过。

- [ ] **Step 5：更新 README 与架构说明中的实际状态**

检查 README.md、docs/架构/ 和 docs/计划/00-实施总路线图.md 对 Workspace overview、Session Agent 版本固定、多 Workspace E2E 的描述。只写本轮命令实际验证的结果；真实模型 Eval 未运行时继续明确标注未运行。

- [ ] **Step 6：提交 Task 5**

~~~powershell
git add tests/apps/api/test_workspace_overview.py apps/workbench/e2e README.md docs/架构 docs/计划/00-实施总路线图.md
git commit -m "test: verify workspace isolation end to end"
~~~

## 最终验收清单

- [ ] AWAIT_CONFIRMATION 和前端角色 OWNER 在工作台源码与测试中均无残留。
- [ ] App 不再向 WorkspaceView 传入硬编码角色。
- [ ] Workspace 页面仅展示 overview API 返回的数据库真实数据。
- [ ] 执行模式保存后使用新 overview 响应展示权威状态。
- [ ] TaskComposer 不调用全局 /agents，只使用当前 Workspace 的启用 Agent。
- [ ] Session 保存绑定时的 agent_version_id，后续新发布版本不会改变既有 Session 的版本。
- [ ] append_message 为 Run 使用 Session 固定 AgentVersion。
- [ ] 非成员、跨 Workspace 引用、未启用绑定、未来绑定和多有效绑定均按稳定错误语义拒绝。
- [ ] 两个 Workspace 的店铺、成员、配置和 Agent 版本隔离均有 API/E2E 证据。
- [ ] Runtime 依赖方向不变，商品业务模型仍只存在于 agents/product_operations。
