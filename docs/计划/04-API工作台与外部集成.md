# API、Agent 工作台与外部集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可供内部运营人员使用的 FastAPI + React 工作台，并将 MCP/WeKnora、Mem0 和 Langfuse 作为非权威外部集成接入。

**Architecture:** API 实现命令/查询分离和短事务，Worker 独立推进 RunJob，WebSocket 只发布已提交 AgentEvent。React 工作台不调用模型、MCP 或业务 API；所有外部集成使用 Outbox 或 Tool 边界，不成为授权和 Runtime 状态权威源。

**Tech Stack:** FastAPI, Uvicorn, WebSocket, React, TypeScript, Vite, TanStack Query, Vitest, Testing Library, Playwright, MCP, Mem0 OSS, Langfuse

---

### Task 1: 实现 FastAPI 组合根与登录

**Files:**
- Create: `apps/api/main.py`
- Create: `apps/api/dependencies.py`
- Create: `apps/api/routes/auth.py`
- Create: `apps/api/schemas/auth.py`
- Create: `apps/api/errors.py`
- Test: `tests/apps/api/test_auth.py`

- [ ] **Step 1: 写登录、过期 JWT 和伪造 Actor 测试**

```python
async def test_login_returns_short_lived_token(api_client, seeded_user) -> None:
    response = await api_client.post("/api/auth/login", json={
        "login": seeded_user.login,
        "password": "correct-password",
    })
    assert response.status_code == 200
    assert set(response.json()) == {"access_token", "token_type", "expires_in"}


async def test_actor_header_cannot_impersonate_user(api_client) -> None:
    response = await api_client.get("/api/workspaces", headers={"X-Actor-Id": "admin"})
    assert response.status_code == 401
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/apps/api/test_auth.py -q`

Expected: FAIL because API app does not exist.

- [ ] **Step 3: 实现 App Factory 和 ActorContext 注入**

API 只从 Bearer JWT 获取 `actor_id`，再从 PostgreSQL 读取当前 Membership/Permission/ResourceScope。错误统一返回 `{code, message, request_id}`，不返回异常堆栈。

- [ ] **Step 4: 运行 API 身份测试**

Run: `uv run pytest tests/apps/api/test_auth.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/api tests/apps/api/test_auth.py
git commit -m "feat: add authenticated runtime api"
```

### Task 2: 实现 Workspace、成员与配置 API

**Files:**
- Create: `apps/api/routes/workspaces.py`
- Create: `apps/api/routes/memberships.py`
- Create: `apps/api/routes/configuration.py`
- Create: `apps/api/schemas/workspaces.py`
- Create: `runtime/workspace/service.py`
- Test: `tests/apps/api/test_workspace_permissions.py`

- [ ] **Step 1: 写 Owner/Operator 配置权限测试**

```python
async def test_operator_cannot_change_execution_mode(operator_client, workspace) -> None:
    response = await operator_client.patch(
        f"/api/workspaces/{workspace.id}/execution-mode",
        json={"mode": "AUTO_EXECUTE"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_workspace_cannot_remove_its_last_owner(owner_client, workspace) -> None:
    response = await owner_client.delete(
        f"/api/workspaces/{workspace.id}/memberships/{workspace.only_owner_id}"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_OWNER_REQUIRED"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/apps/api/test_workspace_permissions.py -q`

Expected: FAIL because workspace routes do not exist.

- [ ] **Step 3: 实现命令与查询端点**

端点覆盖 Workspace 列表/详情、Membership 角色、Resource Scope、Action Permission、Execution Mode、Model Connection、Skill Version、MCP Source 和 Business Policy。Secret 写入后只返回掩码、版本和更新时间。每次配置变更在同一应用服务事务中追加 AuditEvent。

- [ ] **Step 4: 运行 Workspace API 测试**

Run: `uv run pytest tests/apps/api/test_workspace_permissions.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/api runtime/workspace tests/apps/api/test_workspace_permissions.py
git commit -m "feat: add workspace configuration api"
```

### Task 3: 实现 Session、Run 和 Request API

**Files:**
- Create: `apps/api/routes/sessions.py`
- Create: `apps/api/routes/runs.py`
- Create: `apps/api/routes/requests.py`
- Create: `apps/api/schemas/sessions.py`
- Create: `apps/api/schemas/runs.py`
- Test: `tests/apps/api/test_run_commands.py`

- [ ] **Step 1: 写活动 Run、私有 Session 和 Request 单次处理测试**

```python
async def test_waiting_run_blocks_new_user_message(session_client, waiting_session) -> None:
    response = await session_client.post(
        f"/api/sessions/{waiting_session.id}/messages",
        json={"content": "start another task"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ACTIVE_RUN_EXISTS"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/apps/api/test_run_commands.py -q`

Expected: FAIL because run routes do not exist.

- [ ] **Step 3: 实现短事务命令**

提供创建 Session、提交 User Message/创建 Run、取消 Run、回答 Input Request、接受/拒绝 Confirmation Request、读取 Session/Run/Message/ToolCall/Event 快照。API 不直接调用 Backend 或 ToolHandler。

- [ ] **Step 4: 运行 Run API 测试**

Run: `uv run pytest tests/apps/api/test_run_commands.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/api tests/apps/api/test_run_commands.py
git commit -m "feat: expose session and run commands"
```

### Task 4: 实现 AgentEvent WebSocket 重放

**Files:**
- Create: `apps/api/routes/events.py`
- Create: `apps/api/event_stream.py`
- Test: `tests/apps/api/test_event_stream.py`

- [ ] **Step 1: 写断线重连和重复去重测试**

```python
async def test_event_stream_replays_after_cursor(event_client, seeded_events) -> None:
    received = await event_client.collect(cursor=seeded_events[1].id)
    assert [event.id for event in received] == [event.id for event in seeded_events[2:]]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/apps/api/test_event_stream.py -q`

Expected: FAIL because event stream does not exist.

- [ ] **Step 3: 实现 at-least-once 事件流**

WebSocket 建立后先从 PostgreSQL 读取 cursor 后事件，再轮询/通知新提交事件。事件只在状态事务提交后可见，客户端使用 `event_id` 去重、`sequence_no` 检测缺口。

- [ ] **Step 4: 运行事件流测试**

Run: `uv run pytest tests/apps/api/test_event_stream.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/api/routes/events.py apps/api/event_stream.py tests/apps/api/test_event_stream.py
git commit -m "feat: stream durable agent events"
```

### Task 5: 实现 Worker 进程入口与健康状态

**Files:**
- Create: `apps/worker/main.py`
- Create: `apps/worker/settings.py`
- Create: `apps/worker/health.py`
- Test: `tests/apps/worker/test_worker_lifecycle.py`

- [ ] **Step 1: 写启动校验、优雅停止和租约心跳测试**

```python
async def test_worker_stops_claiming_and_finishes_current_safe_point(worker_host) -> None:
    running = await worker_host.start_with_job(long_running_job())
    await worker_host.request_shutdown()
    assert running.heartbeat_count > 0
    assert worker_host.claimed_after_shutdown == 0
    assert await worker_host.wait_stopped(timeout=5) is True
```

- [ ] **Step 2: 运行 `uv run pytest tests/apps/worker -q` 确认测试因 Worker 入口缺失而失败**

- [ ] **Step 3: 实现 Worker Host**

Worker 启动时运行 RuntimeAssembly 校验，循环领取 Job，为执行中 Job 续租，收到停止信号后不再领取新 Job，等待当前安全点或主动放弃租约。Health 分开报告 process alive、database reachable 和 assembly valid。

- [ ] **Step 4: 运行 `uv run pytest tests/apps/worker -q`**

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add apps/worker tests/apps/worker
git commit -m "feat: add durable runtime worker"
```

### Task 6: 接入 MCP 与 WeKnora Source

**Files:**
- Modify: `pyproject.toml`
- Create: `adapters/mcp/client.py`
- Create: `adapters/mcp/source_sync.py`
- Create: `adapters/mcp/tool_handler.py`
- Create: `adapters/mcp/security.py`
- Test: `tests/adapters/mcp/test_source_sync.py`
- Test: `tests/adapters/mcp/test_tool_security.py`

- [ ] **Step 1: 写 Schema 版本化、未分类 Tool 禁用和不可信结果测试**

```python
async def test_new_mcp_schema_requires_local_reclassification(source_sync) -> None:
    first = await source_sync.sync(schema_version_one())
    second = await source_sync.sync(schema_version_two())
    assert second.tool_version_id != first.tool_version_id
    assert second.enabled is False
    assert second.disabled_reason == "LOCAL_CLASSIFICATION_REQUIRED"


@pytest.mark.parametrize(
    ("knowledge_required", "expected"),
    [(False, "DEGRADED"), (True, "SOURCE_UNAVAILABLE")],
)
async def test_weknora_outage_respects_source_requirement(
    mcp_tool_handler, knowledge_required, expected
) -> None:
    result = await mcp_tool_handler.invoke(
        unavailable_weknora_tool(required=knowledge_required)
    )
    assert result.code == expected
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/adapters/mcp -q`

Expected: FAIL because MCP adapter does not exist.

- [ ] **Step 3: 实现远程 HTTPS 与预注册 stdio Source**

`pyproject.toml` 增加 MCP Python SDK。Source Sync 将远程 Schema 保存为新的 Workspace ToolVersion，不接受 MCP Server 声明的 effect/permission。本地 stdio 只通过部署级 Server Key 选择，不从前端接受命令。WeKnora 独立部署，本仓库只保存连接配置并将其作为普通 MCP Source 接入；检索结果只进入 Tool Message，不复制实现其知识库能力。

- [ ] **Step 4: 运行 MCP 测试**

Run: `uv run pytest tests/adapters/mcp -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml adapters/mcp tests/adapters/mcp
git commit -m "feat: integrate secured mcp sources"
```

### Task 7: 接入 Mem0 长期记忆 Outbox

**Files:**
- Modify: `pyproject.toml`
- Create: `runtime/memory/service.py`
- Create: `runtime/memory/lifecycle.py`
- Create: `runtime/integrations/outbox_worker.py`
- Create: `adapters/mem0/client.py`
- Create: `adapters/mem0/outbox_handler.py`
- Create: `apps/api/routes/memories.py`
- Create: `apps/api/schemas/memories.py`
- Modify: `apps/api/main.py`
- Test: `tests/runtime/memory/test_memory_policy.py`
- Test: `tests/runtime/memory/test_memory_lifecycle.py`
- Test: `tests/adapters/mem0/test_outbox.py`
- Test: `tests/apps/api/test_memories.py`

- [ ] **Step 1: 写禁止记忆、生命周期和同步失败测试**

```python
@pytest.mark.parametrize("kind", ["permission", "resource_scope", "price_rule", "credential"])
def test_execution_critical_facts_are_not_memories(memory_service, kind: str) -> None:
    assert memory_service.accept(memory_candidate(kind=kind)) is False


async def test_mem0_failure_does_not_rollback_completed_run(outbox_worker, completed_run) -> None:
    await outbox_worker.process_with_failure("MEMORY_UPSERT")
    assert await completed_run.reload_status() == RunStatus.COMPLETED


async def test_expired_outbox_lease_is_reclaimable(outbox_worker, expired_outbox) -> None:
    lease = await outbox_worker.claim("worker-b")
    assert lease.outbox_id == expired_outbox.id
    assert lease.lease_token != expired_outbox.lease_token


async def test_correction_supersedes_previous_memory(memory_service, active_memory) -> None:
    corrected = await memory_service.correct(active_memory.id, content="prefer cautious changes")
    assert corrected.supersedes_id == active_memory.id
    assert (await active_memory.reload()).lifecycle_status == "SUPERSEDED"


async def test_deleted_memory_is_filtered_even_if_mem0_returns_it(memory_service, active_memory) -> None:
    await memory_service.delete(active_memory.id, actor_id=active_memory.actor_id)
    recalled = await memory_service.filter_retrieval([mem0_hit(active_memory.id)])
    assert recalled == []


async def test_actor_cannot_delete_another_users_memory(memory_service, active_memory) -> None:
    with pytest.raises(MemoryAccessDenied):
        await memory_service.delete(active_memory.id, actor_id=uuid4())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/memory tests/adapters/mem0 tests/apps/api/test_memories.py -q`

Expected: FAIL because memory integration does not exist.

- [ ] **Step 3: 实现权威 MemoryRecord 与异步同步**

`pyproject.toml` 增加 Mem0 OSS 客户端依赖。Runtime 只保存已确认的偏好/反馈/决策/经验。Memory Record 使用 `subject_key/revision/supersedes_id`、生命周期状态、同步状态和有效期；纠正创建新 Revision 并替代旧记录，删除写 Tombstone。应用服务与 API 仅允许当前 Actor 查询、纠正和删除自己的记忆，不实现模型自动冲突裁决。Worker 维护任务调用 `expire_due()` 把到期记录改为 `EXPIRED`。写入和删除分别产生 `MEMORY_UPSERT/MEMORY_DELETE` Outbox。通用 OutboxWorker 使用短租约领取 `PENDING`，失败后按预算延迟重试或置 `DEAD`，不修改 Run。Mem0 检索结果必须按本地 ID、Revision、生命周期和有效期过滤后，才以带来源的不可信数据注入 Context。

- [ ] **Step 4: 运行 Memory 测试**

Run: `uv run pytest tests/runtime/memory tests/adapters/mem0 tests/apps/api/test_memories.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml runtime/memory runtime/integrations adapters/mem0 apps/api tests/runtime/memory tests/adapters/mem0 tests/apps/api/test_memories.py
git commit -m "feat: add durable memory lifecycle"
```

### Task 8: 接入 Langfuse Trace Outbox

**Files:**
- Modify: `pyproject.toml`
- Create: `adapters/langfuse/exporter.py`
- Create: `adapters/langfuse/outbox_handler.py`
- Create: `runtime/observability/redaction.py`
- Test: `tests/adapters/langfuse/test_trace_export.py`

- [ ] **Step 1: 写 Trace 脱敏与不影响 Run 测试**

```python
async def test_trace_payload_is_redacted(trace_exporter) -> None:
    payload = trace_exporter.build_payload(event_with_secret("sk-sensitive"))
    assert "sk-sensitive" not in json.dumps(payload)
    assert payload["attributes"]["run_id"] == "run-1"


async def test_langfuse_failure_never_changes_run(trace_outbox, completed_run) -> None:
    await trace_outbox.process_with_failure(completed_run.id)
    assert await completed_run.reload_status() == RunStatus.COMPLETED
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/adapters/langfuse -q`

Expected: FAIL because Langfuse adapter does not exist.

- [ ] **Step 3: 实现异步 Trace 导出**

`pyproject.toml` 增加 Langfuse Python SDK。Run/Backend/Tool/Request 事件转换为关联 Trace，但导出前必须过字段级脱敏。`TRACE_EXPORT` 失败只改变 Outbox 状态，不改变 Runtime 权威状态。

- [ ] **Step 4: 运行 Trace 测试**

Run: `uv run pytest tests/adapters/langfuse -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml adapters/langfuse runtime/observability tests/adapters/langfuse
git commit -m "feat: export redacted langfuse traces"
```

### Task 9: 建立 React 工作台骨架与 Workspace 视图

**Files:**
- Create: `apps/workbench/package.json`
- Create: `apps/workbench/vite.config.ts`
- Create: `apps/workbench/src/main.tsx`
- Create: `apps/workbench/src/app/App.tsx`
- Create: `apps/workbench/src/api/client.ts`
- Create: `apps/workbench/src/features/workspaces/WorkspacePage.tsx`
- Create: `apps/workbench/src/features/workspaces/WorkspacePage.test.tsx`

- [ ] **Step 1: 写 Workspace 权限化配置界面测试**

```tsx
it("hides configuration controls from operators", async () => {
  render(<WorkspacePage currentRole="OPERATOR" />)
  expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument()
  expect(screen.queryByRole("button", { name: "编辑执行模式" })).not.toBeInTheDocument()
})
```

- [ ] **Step 2: 安装前端依赖并确认测试失败**

Run:

```powershell
npm --prefix apps/workbench install
npm --prefix apps/workbench run test -- --run
```

Expected: FAIL because WorkspacePage is not implemented.

- [ ] **Step 3: 实现工作台 Shell 和 Workspace 配置**

左侧功能栏只包含 Workspace、Sessions 和 Settings；Workspace 页展示店铺、Agent、Model、Skill、Source、Execution Mode 和成员范围。不固定单店作为全局上下文。Owner 配置功能与 Operator 使用功能按当前权限显示。

- [ ] **Step 4: 运行组件测试和 build**

Run:

```powershell
npm --prefix apps/workbench run test -- --run
npm --prefix apps/workbench run build
```

Expected: 测试和 build 退出码为 0。

- [ ] **Step 5: 提交**

```powershell
git add apps/workbench
git commit -m "feat: add agent workbench workspace shell"
```

### Task 10: 实现 Session 对话、运行状态与人机交互

**Files:**
- Create: `apps/workbench/src/features/sessions/SessionPage.tsx`
- Create: `apps/workbench/src/features/sessions/useAgentEvents.ts`
- Create: `apps/workbench/src/features/sessions/InputRequestCard.tsx`
- Create: `apps/workbench/src/features/sessions/ConfirmationCard.tsx`
- Create: `apps/workbench/src/features/sessions/ToolCallCard.tsx`
- Create: `apps/workbench/src/features/sessions/eventReducer.ts`
- Create: `apps/workbench/src/features/sessions/eventReducer.test.ts`
- Create: `apps/workbench/src/features/sessions/SessionPage.test.tsx`

- [ ] **Step 1: 写 Input/Confirmation 分离与 EventReducer 测试**

```tsx
it("renders input and confirmation as different interactions", async () => {
  render(<SessionPage sessionId="session-1" />)
  expect(await screen.findByText("请选择操作店铺")).toBeInTheDocument()
  expect(screen.queryByRole("button", { name: "确认执行" })).not.toBeInTheDocument()
})


it("stops on an event gap and never reverses a terminal run", () => {
  const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"))
  const completed = reduceAgentEvent(baseline, event(5, "RUN_COMPLETED"))
  const withGap = reduceAgentEvent(completed, event(7, "RUN_RUNNING"))
  expect(withGap.needsSnapshot).toBe(true)
  expect(withGap.runs["run-1"].status).toBe("COMPLETED")
  const late = reduceAgentEvent(withGap, event(4, "RUN_RUNNING"))
  expect(late.runs["run-1"].status).toBe("COMPLETED")
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix apps/workbench run test -- --run`

Expected: FAIL because Session components do not exist.

- [ ] **Step 3: 实现对话与嵌入式 Run 状态**

中间主区域展示消息、ToolCall、ToolResult、Input Request、Confirmation Request、错误和最终结果；不建设独立右侧 Run 视图。`EventReducer` 按 event ID 去重、按 Run 序号推进、忽略旧序号、检测缺口后停止消费并拉取带 cursor 的新快照，终态不可被迟到事件逆转。Reducer 只维护 UI 投影，不回写 Runtime 状态。WebSocket 断开后先取消息快照，再从 cursor 重放；编辑器在存在活动等待 Run 时禁止新普通任务。

- [ ] **Step 4: 运行前端检查**

Run:

```powershell
npm --prefix apps/workbench run lint
npm --prefix apps/workbench run test -- --run
npm --prefix apps/workbench run build
```

Expected: 所有命令退出码为 0。

- [ ] **Step 5: 提交**

```powershell
git add apps/workbench/src/features/sessions
git commit -m "feat: add interactive agent session workspace"
```

### Task 11: 执行阶段 4 Gate

**Files:**
- Create: `apps/workbench/e2e/product-price-flow.spec.ts`
- Create: `apps/workbench/e2e/fixtures/runtime.ts`
- Create: `apps/workbench/playwright.config.ts`
- Modify: `docker-compose.yml`
- Create: `docs/开发/外部集成.md`

- [ ] **Step 1: 写工作台关键 E2E**

E2E 使用 ScriptedBackend 和 Mock Commerce Adapter，完成登录、选 Workspace、创建 Session、缺店铺 Input Request、写前确认、改价、重复事件去重、事件缺口触发快照、断线重连和最终价格验证。

```typescript
test("resumes a price update after input, confirmation, and reconnect", async ({ page }) => {
  await loginAndOpenWorkspace(page, "品牌一")
  await submitTask(page, "把 SKU-001 改成 99 元")
  await page.getByRole("button", { name: "淘宝旗舰店" }).click()
  await page.getByRole("button", { name: "确认执行" }).click()
  await page.reload()
  await expect(page.getByText("SKU-001 当前价格：99.00 元")).toBeVisible()
})


test("reloads a snapshot when an event sequence has a gap", async ({ page }) => {
  const runtime = await RuntimeFixture.attach(page)
  await runtime.seedSnapshot({ runId: "run-1", lastSequenceNo: 4, status: "RUNNING" })
  await runtime.pushEvent({ id: "e-5", runId: "run-1", sequenceNo: 5, type: "RUN_COMPLETED" })
  await runtime.pushEvent({ id: "e-7", runId: "run-1", sequenceNo: 7, type: "RUN_RUNNING" })
  await expect.poll(() => runtime.snapshotRequestCount("run-1")).toBe(2)
  await expect(page.getByText("已完成")).toBeVisible()
})
```

- [ ] **Step 2: 启动完整服务并运行全量验证**

Run:

```powershell
docker compose up -d postgres api worker
uv run pytest tests/apps tests/adapters tests/runtime tests/agents -q
npm --prefix apps/workbench run lint
npm --prefix apps/workbench run test -- --run
npm --prefix apps/workbench run build
npm --prefix apps/workbench run test:e2e
git diff --check
```

Expected: 所有命令退出码为 0，E2E 无重试后侥幸通过。

- [ ] **Step 3: 提交**

```powershell
git add apps/workbench/e2e apps/workbench/playwright.config.ts docker-compose.yml docs/开发/外部集成.md
git commit -m "test: verify workbench end-to-end flow"
```
