# Runtime 执行内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Provider 无关的 Agent Harness、持久 Run 状态机、PreToolUse、人机交互、工具执行、幂等回查和故障恢复。

**Architecture:** API 只创建命令和查询状态，Worker 从 PostgreSQL RunJob 租约领取任务。AgentBackend 只处理模型协议，Runtime 是唯一的状态迁移和重试决策者；任何外部写入结果不确定时必须先 Reconciliation。

**Tech Stack:** asyncio, Pydantic v2, SQLAlchemy 2, PostgreSQL leases, OpenAI SDK, Anthropic SDK, JSON Schema, pytest

---

### Task 1: 实现 AgentBackend 协议与 ScriptedBackend

**Files:**
- Modify: `pyproject.toml`
- Create: `runtime/backend/protocol.py`
- Create: `runtime/backend/scripted.py`
- Create: `adapters/model_backends/openai_backend.py`
- Create: `adapters/model_backends/anthropic_backend.py`
- Test: `tests/runtime/backend/test_scripted_backend.py`
- Test: `tests/adapters/model_backends/test_normalization.py`

- [ ] **Step 1: 写 Backend 事件归一化失败测试**

```python
async def test_scripted_backend_emits_completed_tool_call() -> None:
    backend = ScriptedBackend([
        ToolCallCompleted(call_id="call-1", name="lookup", arguments={"sku": "A-1"}),
        BackendCompleted(finish_reason="tool_calls"),
    ])
    events = [event async for event in backend.stream(make_request(), CancellationToken())]
    assert events[0].name == "lookup"
    assert events[0].arguments == {"sku": "A-1"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/backend tests/adapters/model_backends -q`

Expected: FAIL because backend implementations do not exist.

- [ ] **Step 3: 实现 Protocol 和两类生产 Backend**

```python
class AgentBackend(Protocol):
    def capabilities(self) -> AgentBackendCapabilities:
        raise NotImplementedError

    def stream(
        self,
        request: BackendRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[BackendEvent]:
        raise NotImplementedError
```

`pyproject.toml` 增加 `openai` 和 `anthropic` 依赖。`OpenAIBackend` 和 `AnthropicBackend` 只负责 SDK 调用、流式事件、ToolCall、Usage 和 ProviderError 归一化。只有参数 JSON 完整可解析后才发出 `ToolCallCompleted`。

- [ ] **Step 4: 运行 Backend 测试**

Run: `uv run pytest tests/runtime/backend tests/adapters/model_backends -q`

Expected: 归一化测试全部 PASS，不发起真实网络请求。

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml runtime/backend adapters/model_backends tests/runtime/backend tests/adapters/model_backends
git commit -m "feat: add provider-independent agent backends"
```

### Task 2: 实现版本 Registry 与 RuntimeAssembly

**Files:**
- Create: `runtime/capabilities/registry.py`
- Create: `runtime/capabilities/assembly.py`
- Create: `runtime/capabilities/validation.py`
- Test: `tests/runtime/capabilities/test_assembly.py`

- [ ] **Step 1: 写缺失 Handler、Schema 不一致和 Backend 能力不足测试**

```python
def test_assembly_fails_when_local_tool_has_no_binding() -> None:
    assembly = make_assembly(tool_definitions=[local_tool("lookup")], bindings=[])
    with pytest.raises(AssemblyError, match="MISSING_TOOL_BINDING"):
        assembly.validate()


def test_assembly_fails_on_schema_hash_mismatch() -> None:
    assembly = make_assembly_with_mismatched_schema()
    with pytest.raises(AssemblyError, match="SCHEMA_HASH_MISMATCH"):
        assembly.validate()


async def test_run_pins_resolved_versions_but_not_live_authorization(run_factory) -> None:
    run = await run_factory.create(agent_key="product-maintenance")
    assert run.tool_version_ids
    assert run.skill_version_ids
    assert run.source_version_ids
    assert run.permission_snapshot is not None


async def test_write_uses_current_authorization_after_run_creation(
    run_factory, membership_repository, write_service
) -> None:
    run = await run_factory.create(agent_key="product-maintenance")
    await membership_repository.revoke(run.actor_id, "product.price.write")
    result = await write_service.execute(run.id, make_price_tool_call())
    assert result.denial_code == "PERMISSION_REVOKED"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/capabilities/test_assembly.py -q`

Expected: FAIL because registries do not exist.

- [ ] **Step 3: 实现精确版本解析与启动校验**

`AgentRegistry`、`ToolRegistry`、`BackendRegistry` 只按稳定 Key 和 Version ID 查找。`RuntimeAssembly.validate()` 验证 AgentVersion 需要的 Skill/Tool/Source、Local ToolBinding、Schema Hash、写 Tool Adapter 能力和 Backend Capability。MCP Tool 未经本地 effect/permission 分类时不得启用。Run 创建时将实际解析的 Agent/Skill/Tool/Source/Model 版本写入快照关联表；恢复使用这些版本，但权限、Execution Mode、Business Policy 和资源状态仍在写前读当前值。

- [ ] **Step 4: 运行 Registry 测试**

Run: `uv run pytest tests/runtime/capabilities -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/capabilities tests/runtime/capabilities
git commit -m "feat: validate runtime capability assembly"
```

### Task 3: 实现 AgentEventWriter 与合法状态迁移

**Files:**
- Create: `runtime/events/writer.py`
- Create: `runtime/execution/transitions.py`
- Create: `runtime/execution/run_service.py`
- Test: `tests/runtime/execution/test_transitions.py`
- Test: `tests/runtime/events/test_writer.py`

- [ ] **Step 1: 写非法迁移和并发序号测试**

```python
def test_cancelled_run_cannot_resume() -> None:
    with pytest.raises(InvalidTransition):
        ensure_run_transition(RunStatus.CANCELLED, RunStatus.QUEUED)


async def test_event_sequence_is_unique_under_concurrency(event_writer, seeded_run) -> None:
    events = await asyncio.gather(*[
        event_writer.append(seeded_run.id, "TEST_EVENT", {"index": index})
        for index in range(10)
    ])
    assert sorted(event.sequence_no for event in events) == list(range(1, 11))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/execution/test_transitions.py tests/runtime/events/test_writer.py -q`

Expected: FAIL because transition and writer services do not exist.

- [ ] **Step 3: 实现显式迁移表和事件原子写入**

```python
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({
        RunStatus.WAITING_INPUT,
        RunStatus.WAITING_CONFIRMATION,
        RunStatus.WAITING_RECONCILIATION,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.WAITING_INPUT: frozenset({RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.WAITING_CONFIRMATION: frozenset({RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.WAITING_RECONCILIATION: frozenset({RunStatus.QUEUED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}
```

EventWriter 锁定 Run 或 CAS `next_event_sequence`，将状态变更与 AgentEvent 放在同一事务。

- [ ] **Step 4: 运行状态与事件测试**

Run: `uv run pytest tests/runtime/execution/test_transitions.py tests/runtime/events/test_writer.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/events runtime/execution tests/runtime/events tests/runtime/execution
git commit -m "feat: enforce runtime state transitions"
```

### Task 4: 实现 RunJob 租约、续租与 fencing token

**Files:**
- Create: `runtime/jobs/lease_service.py`
- Create: `runtime/jobs/worker.py`
- Create: `runtime/jobs/sweeper.py`
- Test: `tests/runtime/jobs/test_leases.py`

- [ ] **Step 1: 写并发领取、失租提交和租约回收测试**

```python
async def test_only_one_worker_leases_a_job(lease_service, pending_job) -> None:
    leases = await asyncio.gather(
        lease_service.claim("worker-a"),
        lease_service.claim("worker-b"),
    )
    assert sum(lease is not None for lease in leases) == 1


async def test_stale_lease_token_cannot_commit(lease_service, expired_lease) -> None:
    committed = await lease_service.complete(expired_lease.job_id, "stale-token")
    assert committed is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/jobs/test_leases.py -q`

Expected: FAIL because lease services do not exist.

- [ ] **Step 3: 实现 `FOR UPDATE SKIP LOCKED` 与心跳**

领取事务生成随机 `lease_token`、创建 RunAttempt、更新 Job 为 `LEASED`，并使 Run 从 `QUEUED` CAS 到 `RUNNING`。心跳只能用当前 token 续租。Sweeper 将旧 Attempt 标记 `ABANDONED`；如 Operation 已 `EXECUTING/UNKNOWN`，下一 Job 必须是 `RECONCILE_OPERATION`。

- [ ] **Step 4: 运行租约测试**

Run: `uv run pytest tests/runtime/jobs -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/jobs tests/runtime/jobs
git commit -m "feat: add durable run job leases"
```

### Task 5: 实现 Context 预算、压缩和流式 Message 持久化

**Files:**
- Create: `runtime/orchestration/context.py`
- Create: `runtime/orchestration/context_budget.py`
- Create: `runtime/orchestration/token_estimator.py`
- Create: `runtime/orchestration/summarization.py`
- Create: `runtime/orchestration/messages.py`
- Create: `runtime/orchestration/streaming.py`
- Test: `tests/runtime/orchestration/test_context.py`
- Test: `tests/runtime/orchestration/test_context_budget.py`
- Test: `tests/runtime/orchestration/test_streaming.py`

- [ ] **Step 1: 写 ABORTED、不可信内容和 Context 预算测试**

```python
def test_aborted_assistant_message_is_excluded(context_assembler, conversation) -> None:
    request = context_assembler.build(conversation)
    assert "partial secret answer" not in request.serialized_messages()


def test_tool_result_never_becomes_system_message(context_assembler, malicious_tool_result) -> None:
    request = context_assembler.build_with_tool_result(malicious_tool_result)
    assert all(message.role != "system" for message in request.tool_messages)


async def test_context_compression_preserves_protected_items(context_assembler, long_session) -> None:
    request = await context_assembler.build(long_session, input_budget_tokens=800)
    assert request.contains("runtime safety rules")
    assert request.contains(long_session.current_user_message.content)
    assert request.contains(long_session.pending_confirmation.fingerprint)
    assert request.session_summary.covered_to_sequence < long_session.current_user_message.sequence_no
    assert request.session_summary.provider_role != "system"


async def test_protected_context_over_budget_fails_closed(context_assembler, session) -> None:
    with pytest.raises(ContextBudgetExceeded, match="CONTEXT_BUDGET_EXCEEDED"):
        await context_assembler.build(session, input_budget_tokens=10)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/orchestration/test_context.py tests/runtime/orchestration/test_context_budget.py tests/runtime/orchestration/test_streaming.py -q`

Expected: FAIL because orchestration services do not exist.

- [ ] **Step 3: 实现上下文优先级、Token 预算和结构化 Summary**

Context 顺序固定为 Runtime 安全规则、Agent Prompt/Skill、Workspace 非敏感摘要、已完成消息、当前 Run ToolResult 投影、过滤 Memory、当前用户消息。`ContextBudgetPolicy` 使用注入的 `TokenEstimator`，从 Context Window 扣除最大输出、Tool Schema 和安全余量；安全规则、当前意图、活动 ToolCall 与待处理 Request 标记为 Protected。超限时依次移除低相关 Memory、旧 ToolResult 投影，并用当前固定 AgentBackend 在禁用 Tool Calling 的模式下生成带覆盖区间、Schema 版本和来源 Hash 的 `SESSION_SUMMARY`；Summarizer 按独立输入预算增量合并上一 Summary 与下一段最早消息，不重新提交整段超长历史。该调用计入模型预算，Summary 作为不可信数据而非 Provider System Message 注入。只有 `COMPLETED` Message 进入上下文；流失败时标记 `ABORTED`。Tool/MCP/Memory 统一按不可信数据注入。Summary Schema 校验失败或 Protected Context 自身超限时返回 `CONTEXT_BUDGET_EXCEEDED`，不静默丢内容。

- [ ] **Step 4: 运行 Context 测试**

Run: `uv run pytest tests/runtime/orchestration/test_context.py tests/runtime/orchestration/test_context_budget.py tests/runtime/orchestration/test_streaming.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/orchestration tests/runtime/orchestration
git commit -m "feat: assemble safe persistent model context"
```

### Task 6: 实现固定 PreToolUse 管线

**Files:**
- Create: `runtime/execution/pretool/pipeline.py`
- Create: `runtime/execution/pretool/schema_validation.py`
- Create: `runtime/execution/pretool/decisions.py`
- Create: `runtime/execution/tool_registry.py`
- Test: `tests/runtime/execution/test_pretool_pipeline.py`

- [ ] **Step 1: 写阶段顺序、模型错误和可补充缺字段测试**

```python
async def test_pretool_stages_have_fixed_order(recording_pipeline) -> None:
    await recording_pipeline.evaluate(valid_tool_call())
    assert recording_pipeline.calls == [
        "tool_resolution",
        "schema_validation",
        "input_normalization",
        "argument_provenance",
        "input_requirement",
        "authorization_resource",
        "access_control",
        "business_policy",
        "execution_mode",
        "confirmation_verification",
    ]


async def test_unparseable_arguments_are_model_error(pipeline) -> None:
    decision = await pipeline.evaluate(tool_call(raw_arguments="{"))
    assert decision.kind == "DENY"
    assert decision.reason_code == "INVALID_MODEL_OUTPUT"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/execution/test_pretool_pipeline.py -q`

Expected: FAIL because pipeline does not exist.

- [ ] **Step 3: 实现不可重排的 Pipeline**

Pipeline 返回 `ALLOW/DENY/REQUIRE_INPUT/REQUIRE_CONFIRMATION`。未知 Tool、版本漂移、非 JSON 根结构和未允许字段不生成 Input Request；缺失业务字段可将 Schema Diagnostic 交给 InputRequirementResolver。业务扩展只返回值对象，不获取 Repository。

- [ ] **Step 4: 运行 PreToolUse 测试**

Run: `uv run pytest tests/runtime/execution/test_pretool_pipeline.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/execution/pretool runtime/execution/tool_registry.py tests/runtime/execution/test_pretool_pipeline.py
git commit -m "feat: enforce deterministic pretool pipeline"
```

### Task 7: 实现 Input Request 与 Confirmation Request

**Files:**
- Create: `runtime/interaction/input_service.py`
- Create: `runtime/interaction/confirmation_service.py`
- Create: `runtime/interaction/fingerprints.py`
- Create: `runtime/interaction/expiry.py`
- Test: `tests/runtime/interaction/test_input_requests.py`
- Test: `tests/runtime/interaction/test_confirmation_requests.py`

- [ ] **Step 1: 写单次消费、字段白名单和 TOCTOU 测试**

```python
async def test_input_answer_only_updates_declared_paths(input_service, pending_input) -> None:
    with pytest.raises(InputAnswerRejected, match="UNDECLARED_FIELD"):
        await input_service.answer(pending_input.id, {"store_id": "s1", "price": "1.00"})


async def test_changed_resource_version_invalidates_confirmation(
    confirmation_service, accepted_confirmation
) -> None:
    result = await confirmation_service.verify(
        accepted_confirmation.id,
        current_resource_versions={"listing": 2},
    )
    assert result.valid is False
    assert result.reason_code == "CONFIRMATION_STALE"


async def test_expired_input_cannot_resume_run(input_service, expired_input) -> None:
    with pytest.raises(InputAnswerRejected, match="REQUEST_EXPIRED"):
        await input_service.answer(expired_input.id, {"store_id": "s1"})
    assert await expired_input.reload_status() == RequestStatus.EXPIRED
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/interaction -q`

Expected: FAIL because interaction services do not exist.

- [ ] **Step 3: 实现 Request 事务**

Input Answer 只合并 `field_paths` 声明的字段。Confirmation Fingerprint 使用规范化 JSON，绑定 Actor、Workspace、ToolCall、`operation_id`、参数、资源版本、Tool Schema、Policy 和 Execution Mode。回答使用 `WHERE status='PENDING' AND expires_at > now()` 条件更新，恢复后从 Tool Resolution 重走 PreToolUse。每次创建、回答、接受、拒绝、过期和取消都通过 AuditService 写只追加审计。

- [ ] **Step 4: 运行交互测试**

Run: `uv run pytest tests/runtime/interaction -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/interaction tests/runtime/interaction
git commit -m "feat: add durable input and confirmation requests"
```

### Task 8: 实现 ToolResultProcessor、Operation 和 Reconciliation

**Files:**
- Create: `runtime/execution/tool_executor.py`
- Create: `runtime/execution/tool_result_processor.py`
- Create: `runtime/execution/context_projection.py`
- Create: `runtime/execution/operations.py`
- Create: `runtime/execution/reconciliation.py`
- Test: `tests/runtime/execution/test_tool_result_processor.py`
- Test: `tests/runtime/execution/test_operations.py`
- Test: `tests/runtime/execution/test_reconciliation.py`

- [ ] **Step 1: 写 PostToolUse、响应丢失和 `NOT_APPLIED` 重新校验测试**

```python
async def test_unknown_write_schedules_reconciliation(operation_service) -> None:
    result = await operation_service.record_result(tool_result_unknown())
    assert result.run_status == RunStatus.WAITING_RECONCILIATION
    assert result.next_job_kind == JobKind.RECONCILE_OPERATION


async def test_not_applied_returns_to_validation(reconciliation_service) -> None:
    result = await reconciliation_service.apply_result(ReconcileResult.NOT_APPLIED)
    assert result.tool_call_status == ToolCallStatus.VALIDATING
    assert result.direct_execute is False


async def test_posttool_redacts_and_filters_context_projection(tool_result_processor) -> None:
    processed = await tool_result_processor.process(
        result_with_extra_field_and_secret("sk-sensitive")
    )
    assert "extra_field" not in processed.context_projection.structured_data
    assert "sk-sensitive" not in processed.context_projection.model_dump_json()


async def test_projection_failure_does_not_erase_applied_write(tool_result_processor) -> None:
    processed = await tool_result_processor.process(applied_write_with_invalid_payload())
    assert processed.operation_outcome == OperationOutcome.APPLIED
    assert processed.tool_call_error == "INVALID_TOOL_RESULT"
    assert processed.retry_allowed is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/execution/test_tool_result_processor.py tests/runtime/execution/test_operations.py tests/runtime/execution/test_reconciliation.py -q`

Expected: FAIL because operation services do not exist.

- [ ] **Step 3: 实现固定 PostToolUse 与两阶段写操作**

外部调用前的短事务创建或读取稳定 Operation，写 `EXECUTING` 与 Attempt；提交后调用 Adapter。`ToolResultProcessor` 固定执行协议校验、Operation 结果分类、Output Schema 校验、字段级脱敏、大小限制和 `ContextProjection`；权威执行结果、模型投影与前端安全视图分离。首版不建设 Artifact Store，权威结果超限返回确定错误，模型投影按字段和数组上限确定性裁剪并保存 `truncated/content_hash`。结果事务用 lease token 与 `state_version` CAS 提交状态、脱敏结果和 AgentEvent。`UNKNOWN` 只能进入 Reconciliation；`NOT_APPLIED` 只能回到 `VALIDATING`；已经 `APPLIED` 的写入不能因输出或投影失败被解释为未执行。写操作被允许、拒绝、执行、回查和进入 `UNKNOWN` 都写 AuditEvent。

- [ ] **Step 4: 运行 Operation 与回查测试**

Run: `uv run pytest tests/runtime/execution/test_tool_result_processor.py tests/runtime/execution/test_operations.py tests/runtime/execution/test_reconciliation.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/execution tests/runtime/execution
git commit -m "feat: reconcile uncertain tool writes"
```

### Task 9: 组装 Agent Loop 与限制预算

**Files:**
- Create: `runtime/orchestration/agent_loop.py`
- Create: `runtime/orchestration/limits.py`
- Create: `runtime/orchestration/runtime_host.py`
- Test: `tests/runtime/orchestration/test_agent_loop.py`
- Test: `tests/runtime/orchestration/test_limits.py`

- [ ] **Step 1: 写多 ToolCall 顺序、挂起恢复和限额测试**

```python
async def test_multiple_tool_calls_stop_at_first_waiting_input(agent_loop) -> None:
    result = await agent_loop.advance(run_with_two_tool_calls())
    assert result.persisted_ordinals == [0, 1]
    assert result.processed_ordinals == [0]
    assert result.run_status == RunStatus.WAITING_INPUT


async def test_loop_limit_fails_deterministically(agent_loop) -> None:
    result = await agent_loop.advance(run_at_model_round_limit())
    assert result.failure_code == "LOOP_LIMIT_EXCEEDED"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/orchestration/test_agent_loop.py tests/runtime/orchestration/test_limits.py -q`

Expected: FAIL because AgentLoop does not exist.

- [ ] **Step 3: 实现单线程 ToolCall 执行循环**

AgentLoop 先按 ordinal 持久化所有完整 ToolCall，再顺序处理。任一调用进入 Input/Confirmation/Reconciliation 后停止后续调用。恢复先处理挂起 ToolCall，再处理剩余 ordinal。预算包含模型轮次、ToolCall 数、单 Tool 超时、Run Deadline、Backend 重试和 Reconciliation 次数。

- [ ] **Step 4: 运行 Runtime 单元和集成测试**

Run: `uv run pytest tests/runtime -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add runtime/orchestration tests/runtime/orchestration
git commit -m "feat: complete persistent agent loop"
```

### Task 10: 执行阶段 2 Gate

**Files:**
- Create: `tests/runtime/integration/test_crash_recovery.py`
- Modify: `docs/架构/Runtime执行状态与恢复设计.md`

- [ ] **Step 1: 添加外部写入前后崩溃的故障注入测试**

测试必须覆盖：执行前 Worker 退出、Operation 已改为 `EXECUTING` 后退出、外部写入生效但响应丢失、回查 `UNKNOWN → APPLIED`、旧 Worker 使用失效 fencing token 提交、ToolResult Schema 失败不清除已发生写入、Context 压缩不丢 Protected Context。

```python
async def test_response_loss_reconciles_without_second_write(crash_harness) -> None:
    first = await crash_harness.run_until("AFTER_EXTERNAL_WRITE_BEFORE_RESULT")
    assert first.run.status == RunStatus.WAITING_RECONCILIATION

    recovered = await crash_harness.recover()
    assert recovered.operation.status == OperationStatus.APPLIED
    assert recovered.run.status == RunStatus.COMPLETED
    assert crash_harness.external_write_count == 1
```

- [ ] **Step 2: 运行阶段全量验证**

Run:

```powershell
uv run ruff check runtime adapters tests
uv run mypy runtime adapters
uv run pytest tests/runtime tests/adapters -q
git diff --check
```

Expected: 所有命令退出码为 0，pytest 无 failed/error/skipped。

- [ ] **Step 3: 只将文档中已验证的目标改为实现状态并提交**

```powershell
git add tests/runtime/integration docs/架构/Runtime执行状态与恢复设计.md
git commit -m "test: verify runtime crash recovery"
```
