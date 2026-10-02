# 商品运营参考 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用独立商品域模块、确定性 Mock 平台和五个可组合能力完成“查询 → 对比 → 计划 → 确认 → 改价 → 回查”闭环。

**Architecture:** `agents/product_operations` 定义 Tool、Skill、参数来源和 Business Policy，`adapters/commerce` 实现平台协议，`product_operations` PostgreSQL Schema 保存参考业务状态。Runtime 只看到公开 ToolBinding、PolicyDecision 和 `(resource_type, resource_id)`。

**Tech Stack:** Pydantic v2, SQLAlchemy 2, PostgreSQL, Decimal, pytest, ScriptedBackend

---

### Task 1: 建立商品域 Schema 和可重置 Fixture

**Files:**
- Create: `agents/product_operations/domain/models.py`
- Create: `agents/product_operations/persistence/models.py`
- Create: `agents/product_operations/persistence/repository.py`
- Create: `migrations/versions/0002_product_operations.py`
- Create: `tests/agents/product_operations/test_domain_schema.py`
- Create: `tests/fixtures/product_operations.py`

- [ ] **Step 1: 写唯一约束、Decimal 和版本冲突测试**

```python
async def test_listing_compare_and_set_rejects_stale_version(product_repository, listing) -> None:
    updated = await product_repository.update_price(
        workspace_id=listing.workspace_id,
        listing_id=listing.id,
        target_price=Decimal("99.00"),
        expected_version=listing.version + 1,
    )
    assert updated is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/agents/product_operations/test_domain_schema.py -q`

Expected: FAIL because the product domain does not exist.

- [ ] **Step 3: 实现 Store/Product/Listing/PriceLock 与 Migration**

Price 使用 `Numeric(18, 2)` 和 Python `Decimal`；Listing 使用 `version` CAS；Store 平台枚举只包含 `TAOBAO/DOUYIN`。Fixture 每条用例创建独立 Workspace 和业务状态，用例后事务回滚。

- [ ] **Step 4: 运行 Migration 与领域测试**

Run:

```powershell
uv run alembic upgrade head
uv run pytest tests/agents/product_operations/test_domain_schema.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add agents/product_operations migrations/versions/0002_product_operations.py tests/agents tests/fixtures
git commit -m "feat: add product operations domain"
```

### Task 2: 实现只读商品 Tool

**Files:**
- Create: `agents/product_operations/tools/search_products.py`
- Create: `agents/product_operations/tools/get_product_snapshot.py`
- Create: `agents/product_operations/tools/compare_listing_prices.py`
- Create: `agents/product_operations/tools/definitions.py`
- Test: `tests/agents/product_operations/test_read_tools.py`

- [ ] **Step 1: 写授权店铺过滤和输出 Schema 测试**

```python
async def test_compare_prices_returns_only_authorized_stores(read_tools, operator) -> None:
    result = await read_tools.compare_listing_prices(
        actor=operator,
        sku="SKU-001",
    )
    assert {row.store_id for row in result.items} == {"store-taobao"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/agents/product_operations/test_read_tools.py -q`

Expected: FAIL because read tools do not exist.

- [ ] **Step 3: 实现 3 个 READ ToolBinding**

`search_products`、`get_product_snapshot`、`compare_listing_prices` 全部声明 `effect=READ` 与 `required_permissions={"product.read"}`。查询必须同时使用 `workspace_id` 与授权 Store ID，输出通过本地 Output Schema 白名单过滤。

- [ ] **Step 4: 运行只读 Tool 测试**

Run: `uv run pytest tests/agents/product_operations/test_read_tools.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add agents/product_operations/tools tests/agents/product_operations/test_read_tools.py
git commit -m "feat: add product query tools"
```

### Task 3: 实现参数归一化与 ArgumentProvenance

**Files:**
- Create: `agents/product_operations/input/normalizer.py`
- Create: `agents/product_operations/input/provenance.py`
- Create: `agents/product_operations/input/requirements.py`
- Test: `tests/agents/product_operations/test_argument_provenance.py`

- [ ] **Step 1: 写“模型编店铺”和候选项过滤测试**

```python
def test_model_supplied_store_without_evidence_requires_input(input_resolver) -> None:
    result = input_resolver.resolve(
        user_message="把 SKU-001 改成 99 元",
        arguments={"store_id": "store-taobao", "sku": "SKU-001", "target_price": "99.00"},
    )
    assert result.requirement.field_paths == ("store_id",)
    assert result.provenance["store_id"] == ArgumentProvenance.UNTRUSTED


def test_store_options_are_limited_to_actor_scope(input_resolver) -> None:
    result = input_resolver.store_requirement(actor_with_taobao_only())
    assert [option.value for option in result.options] == ["store-taobao"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/agents/product_operations/test_argument_provenance.py -q`

Expected: FAIL because input resolvers do not exist.

- [ ] **Step 3: 实现确定性证据规则**

Provenance 只由当前用户文本精确证据、结构化 Request 回答、权威读取和确定性计算产生。`store_id` 只接受 `USER_EXPLICIT/USER_CONFIRMED`；`sku` 允许唯一 `SYSTEM_RESOLVED`；`target_price` 允许 `DETERMINISTIC_DERIVED`。旧对话、模型 `source/confidence` 和“只有一家店”均不是可信证据。

- [ ] **Step 4: 运行 Provenance 测试**

Run: `uv run pytest tests/agents/product_operations/test_argument_provenance.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add agents/product_operations/input tests/agents/product_operations/test_argument_provenance.py
git commit -m "feat: validate product argument provenance"
```

### Task 4: 实现资源解析和价格 Policy

**Files:**
- Create: `agents/product_operations/policy/resources.py`
- Create: `agents/product_operations/policy/price_policy.py`
- Create: `agents/product_operations/policy/models.py`
- Test: `tests/agents/product_operations/test_price_policy.py`

- [ ] **Step 1: 写越权、低于底价、锁价和幅度超限测试**

```python
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("below_floor", "PRICE_BELOW_FLOOR"),
        ("active_lock", "PRICE_LOCKED"),
        ("change_too_large", "PRICE_CHANGE_LIMIT_EXCEEDED"),
    ],
)
def test_price_policy_denies_unsafe_writes(price_policy, case: str, reason: str) -> None:
    decision = price_policy.evaluate(price_case(case))
    assert decision.allowed is False
    assert decision.reason_code == reason
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/agents/product_operations/test_price_policy.py -q`

Expected: FAIL because policy modules do not exist.

- [ ] **Step 3: 实现结构化 PolicyDecision**

AuthorizationResourceResolver 只返回 `ResourceRef(type="store", id=store_id)`。PricePolicy 从 PostgreSQL 读取 Listing/PriceLock/当前 PolicyVersion，依次校验状态、Decimal/币种、最低允许价、锁价、单次变动硬阈值和 Listing Version。Policy 不从 Prompt、Memory 或 WeKnora 读取授权事实。

- [ ] **Step 4: 运行 Policy 测试**

Run: `uv run pytest tests/agents/product_operations/test_price_policy.py -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add agents/product_operations/policy tests/agents/product_operations/test_price_policy.py
git commit -m "feat: enforce product price policy"
```

### Task 5: 实现 Mock Commerce Adapter 的幂等写入与回查

**Files:**
- Create: `adapters/commerce/protocol.py`
- Create: `adapters/commerce/mock_adapter.py`
- Create: `adapters/commerce/faults.py`
- Test: `tests/adapters/commerce/test_mock_adapter.py`

- [ ] **Step 1: 写幂等、CAS、响应丢失和回查测试**

```python
async def test_response_loss_does_not_duplicate_price_write(mock_adapter, listing) -> None:
    mock_adapter.faults.inject("response_lost_after_commit")
    first = await mock_adapter.execute(update_command(listing, operation_id="op-1"))
    assert first.status == "UNKNOWN"
    reconciled = await mock_adapter.reconcile("op-1", expected_price=Decimal("99.00"))
    assert reconciled == ReconcileResult.APPLIED
    assert await mock_adapter.count_logical_writes("op-1") == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/adapters/commerce/test_mock_adapter.py -q`

Expected: FAIL because commerce adapter does not exist.

- [ ] **Step 3: 实现可重现的 Mock Adapter**

Adapter 使用 `operation_id`/idempotency key 去重，使用 `expected_version` CAS 写入，写后重读 Listing。FaultInjector 支持 `timeout_before_write`、`response_lost_after_commit`、`temporary_reconcile_unknown` 和 `permanent_failure`，不使用随机概率。

- [ ] **Step 4: 运行 Adapter 测试**

Run: `uv run pytest tests/adapters/commerce -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add adapters/commerce tests/adapters/commerce
git commit -m "feat: add deterministic commerce adapter"
```

### Task 6: 组装 `update_price` Tool 与 ProductMaintenance Agent

**Files:**
- Create: `agents/product_operations/tools/update_price.py`
- Create: `agents/product_operations/agent.py`
- Create: `agents/product_operations/skill.py`
- Create: `agents/product_operations/prompt.py`
- Create: `apps/worker/assembly.py`
- Test: `tests/agents/product_operations/test_agent_assembly.py`
- Test: `tests/agents/product_operations/test_price_flow.py`

- [ ] **Step 1: 写端到端 ScriptedBackend 改价闭环测试**

```python
async def test_confirmed_price_update_reaches_authoritative_state(product_runtime) -> None:
    run = await product_runtime.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    run = await product_runtime.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED
    assert await product_runtime.authoritative_price("store-taobao", "SKU-001") == Decimal("99.00")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/agents/product_operations/test_agent_assembly.py tests/agents/product_operations/test_price_flow.py -q`

Expected: FAIL because product Agent assembly is incomplete.

- [ ] **Step 3: 注册四个商品 Tool 与一个 Skill**

`update_price` 模型可见字段只有 `store_id/sku/target_price`；currency、Listing Version、Policy Version、`operation_id` 和 idempotency key 由服务端补全。Agent Prompt 只描述任务方法，不复制代码 Policy。Worker 是组合根，Runtime 不导入该 Agent。

- [ ] **Step 4: 运行商品 Agent 测试**

Run: `uv run pytest tests/agents/product_operations -q`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add agents/product_operations apps/worker/assembly.py tests/agents/product_operations
git commit -m "feat: assemble product operations agent"
```

### Task 7: 执行阶段 3 Gate

**Files:**
- Create: `tests/agents/product_operations/test_safety_matrix.py`
- Modify: `docs/架构/商品运营参考Agent设计.md`

- [ ] **Step 1: 添加安全矩阵测试**

矩阵至少包含：缺店铺、猜测店铺、越出店铺范围、缺写权限、`READ_ONLY`、低于底价、活动锁价、确认后权限撤销、确认后 Listing Version 变化、响应丢失和重复提交。每条同时断言 ToolCall 状态与权威最终状态。

```python
@pytest.mark.parametrize(
    ("case_id", "expected_status", "expected_writes"),
    [
        ("missing_store", ToolCallStatus.WAITING_INPUT, 0),
        ("guessed_store", ToolCallStatus.WAITING_INPUT, 0),
        ("out_of_scope_store", ToolCallStatus.DENIED, 0),
        ("below_floor_price", ToolCallStatus.DENIED, 0),
        ("response_lost", ToolCallStatus.SUCCEEDED, 1),
        ("duplicate_submit", ToolCallStatus.SUCCEEDED, 1),
    ],
)
async def test_price_safety_matrix(
    safety_case_runner, case_id, expected_status, expected_writes
) -> None:
    result = await safety_case_runner.run(case_id)
    assert result.tool_call_status == expected_status
    assert result.authoritative_write_count == expected_writes
```

- [ ] **Step 2: 运行阶段全量验证**

Run:

```powershell
uv run ruff check agents adapters tests/agents tests/adapters
uv run mypy agents adapters
uv run pytest tests/agents tests/adapters tests/runtime -q
git diff --check
```

Expected: 所有命令退出码为 0，安全矩阵无危险或重复写入。

- [ ] **Step 3: 记录实际用例数并提交**

```powershell
git add tests/agents/product_operations/test_safety_matrix.py docs/架构/商品运营参考Agent设计.md
git commit -m "test: verify product agent safety matrix"
```
