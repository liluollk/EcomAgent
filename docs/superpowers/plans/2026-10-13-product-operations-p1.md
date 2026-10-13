# 商品运营模块化 P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将第一版改价 Agent 重组为包含商品发现、价格、库存和刊登能力的 `ProductOperationsAgent`，并保持 Runtime 通用、写操作可确认、可幂等恢复、可审计和可评测。

**Architecture:** 保持模块化单体。业务代码按 Catalog、Pricing、Inventory、Listing 四个能力模块组织，模块拥有 Tool、Service、Policy、Gateway 和 Reconciler；Runtime 只提供版本固定、PreToolUse、Operation、确认和 Reconciliation。先补齐通用 Registry 多版本与写操作 prepare/execute 契约，再迁移价格闭环，最后增加库存和刊登。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、PostgreSQL、Alembic、pytest、httpx、ruff、mypy

---

## 实施约束

- 设计事实源：`docs/superpowers/specs/2026-10-13-product-operations-p1-design.md`。
- Skill 只做新 Tool 名称的兼容性版本更新，不实施 ADR-001 的目标模型。
- 每个任务先写失败测试，再写最小实现；不得在一个提交中混入后续任务。
- 旧 AgentVersion、SkillVersion 和 ToolVersion 不删除、不覆盖；新 Run 只使用新版本，旧版本仅供恢复。
- 不修改简历正文，不继承旧真实模型评测数字。
- 不实现第二个真实平台、动态 `CapabilityRouter`、Workflow Engine 或 Multi-Agent。
- 以下命令默认在 `C:\Users\L3553\Desktop\agent-runtime` 执行。

## 目标文件结构

```text
agents/product_operations/
  catalog/{__init__.py,models.py,service.py,tools.py}
  pricing/{__init__.py,models.py,gateway.py,policy.py,service.py,tools.py,reconciliation.py}
  inventory/{__init__.py,models.py,gateway.py,policy.py,service.py,tools.py,reconciliation.py}
  listing/{__init__.py,models.py,gateway.py,policy.py,service.py,tools.py,reconciliation.py}
  shared/{__init__.py,errors.py,write_contracts.py,reconciliation.py}
  persistence/{models.py,repository.py}
  agent.py
  prompt.py
  skill.py

adapters/commerce/
  mock_adapter.py
  rest_adapter.py

composition/
  product_operations.py
  product_operations_bindings.py
  product_operations_recovery.py
  provisioning/product_operations.py

eval/
  capability_manifest.py
  config.py
```

旧 `agents/product_operations/tools/*` 和 `agents/product_operations/policy/*` 在迁移期保留兼容实现；仅当历史 ToolVersion 已能由兼容绑定恢复后，才删除不再被引用的实现。

---

### Task 1: 让 Runtime Registry 支持同 stable key 的多个版本

**Files:**
- Modify: `runtime/capabilities/registry.py`
- Modify: `runtime/capabilities/assembly.py`
- Modify: `composition/product_operations.py`
- Modify: `tests/runtime/capabilities/test_assembly.py`
- Modify: `tests/agents/product_operations/test_agent_assembly.py`
- Modify: `tests/architecture/test_dependency_boundaries.py`

- [ ] **Step 1: 写多版本 Registry 失败测试**

在 `tests/runtime/capabilities/test_assembly.py` 增加：

```python
def test_registry_keeps_current_and_compatible_versions() -> None:
    registry = ToolRegistry()
    v1 = tool_record(stable_key="set_listing_price", version=1)
    v2 = tool_record(stable_key="set_listing_price", version=2)

    registry.register(v1, make_current=False)
    registry.register(v2, make_current=True)

    assert registry.find_by_key("set_listing_price") == v2
    assert registry.find_by_version_id(v1.version_id) == v1
    assert registry.find_by_version_id(v2.version_id) == v2
    assert registry.find_by_key_version("set_listing_price", 1) == v1


def test_registry_rejects_same_key_and_version_with_different_ids() -> None:
    registry = ToolRegistry()
    registry.register(tool_record(stable_key="search_products", version=3))
    with pytest.raises(AssemblyError, match="REGISTRY_VERSION_CONFLICT"):
        registry.register(tool_record(stable_key="search_products", version=3))
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/capabilities/test_assembly.py -q
```

Expected: FAIL，提示 `register()` 不接受 `make_current` 或缺少 `find_by_key_version`。

- [ ] **Step 3: 实现显式当前版本和兼容版本索引**

将 `_VersionedRegistry` 改为：

```python
class _VersionedRegistry[R: VersionedRecord]:
    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._current_by_key: dict[str, R] = {}
        self._by_key_version: dict[tuple[str, int], R] = {}
        self._by_version_id: dict[UUID, R] = {}

    def register(self, record: R, *, make_current: bool = True) -> None:
        key = (record.stable_key, record.version)
        existing = self._by_key_version.get(key)
        if existing is not None and existing.version_id != record.version_id:
            raise AssemblyError(
                REGISTRY_VERSION_CONFLICT,
                f"{self._kind} {record.stable_key} v{record.version} 指向不同 Version ID",
            )
        by_id = self._by_version_id.get(record.version_id)
        if by_id is not None and by_id != record:
            raise AssemblyError(
                REGISTRY_VERSION_CONFLICT,
                f"Version ID {record.version_id} 已被其他 {self._kind} 版本占用",
            )
        self._by_key_version[key] = record
        self._by_version_id[record.version_id] = record
        if make_current:
            self._current_by_key[record.stable_key] = record

    def find_by_key(self, stable_key: str) -> R | None:
        return self._current_by_key.get(stable_key)

    def find_by_key_version(self, stable_key: str, version: int) -> R | None:
        return self._by_key_version.get((stable_key, version))

    def find_by_version_id(self, version_id: UUID) -> R | None:
        return self._by_version_id.get(version_id)

    def records(self) -> tuple[R, ...]:
        return tuple(self._current_by_key.values())

    def all_records(self) -> tuple[R, ...]:
        return tuple(self._by_version_id.values())
```

- [ ] **Step 4: 将 ToolBinding 改为按 ToolVersion ID 绑定**

```python
class ToolRegistry(_VersionedRegistry[ToolVersionRecord]):
    def __init__(self) -> None:
        super().__init__("Tool")
        self._bindings_by_version_id: dict[UUID, ToolBinding] = {}

    def bind(self, version_id: UUID, binding: ToolBinding) -> None:
        if version_id in self._bindings_by_version_id:
            raise AssemblyError(REGISTRY_DUPLICATE_KEY, f"ToolVersion {version_id} 重复绑定")
        self._bindings_by_version_id[version_id] = binding

    def binding_for_version(self, version_id: UUID) -> ToolBinding | None:
        return self._bindings_by_version_id.get(version_id)

    def binding_for_key(self, stable_key: str) -> ToolBinding | None:
        record = self.find_by_key(stable_key)
        return None if record is None else self.binding_for_version(record.version_id)


@dataclass(frozen=True)
class VersionedToolBinding:
    version_id: UUID
    binding: ToolBinding
    make_current: bool
```

同步修改 `RuntimeAssembly` 校验和 `_RegistryToolPins`：创建新 Run 时读取当前版本；按 Version ID 恢复时遍历 `all_records()` 并调用 `binding_for_version()`。

- [ ] **Step 5: 迁移全部现有 `tool_registry.bind(...)` 调用**

统一改为：

```python
tool_registry.register(record, make_current=True)
tool_registry.bind(record.version_id, binding)
```

兼容版本使用：

```python
tool_registry.register(legacy_record, make_current=False)
tool_registry.bind(legacy_record.version_id, legacy_binding)
```

- [ ] **Step 6: 运行 Runtime 与架构测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/capabilities tests/agents/product_operations/test_agent_assembly.py tests/architecture/test_dependency_boundaries.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add runtime/capabilities/registry.py runtime/capabilities/assembly.py composition/product_operations.py tests/runtime/capabilities/test_assembly.py tests/agents/product_operations/test_agent_assembly.py tests/architecture/test_dependency_boundaries.py
git commit -m "refactor: support versioned capability registry"
```

---

### Task 2: 建立写操作 prepare/execute 通用契约

**Files:**
- Modify: `runtime/execution/tool_executor.py`
- Modify: `runtime/execution/operations.py`
- Modify: `tests/runtime/execution/test_operations.py`
- Create: `tests/runtime/execution/test_prepared_writes.py`
- Modify: `agents/product_operations/tools/update_price.py`
- Modify: `composition/product_operations.py`

- [ ] **Step 1: 写“预期状态必须先于外部调用持久化”失败测试**

```python
async def test_expected_state_is_committed_before_external_write(runtime_write_fixture) -> None:
    handler = RecordingPreparedHandler(
        expected_state={"operation_type": "SET_LISTING_PRICE", "target_price": "99.00"}
    )
    outcome = await runtime_write_fixture.execute(handler=handler)

    assert handler.expected_state_seen_before_execute == {
        "operation_type": "SET_LISTING_PRICE",
        "target_price": "99.00",
    }
    assert outcome.operation.expected_state == handler.expected_state_seen_before_execute
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/execution/test_prepared_writes.py -q
```

Expected: FAIL，缺少 `PreparedToolWrite` 和 `prepare()`。

- [ ] **Step 3: 定义通用准备结果和 Handler 协议**

在 `runtime/execution/tool_executor.py` 定义：

```python
@dataclass(frozen=True)
class PreparedWriteRejection:
    status: Literal["FAILED"]
    error_code: str
    context: Mapping[str, Any]          # 最小审计上下文,不参与 Reconciliation
    payload: Mapping[str, Any] | None = None
    retryable: bool = False             # 默认不可重试(Task 2.2)


@dataclass(frozen=True)
class PreparedToolWrite:
    command: object | None
    expected_state: Mapping[str, Any] | None
    request_fingerprint: str
    rejection: PreparedWriteRejection | None = None
    # __post_init__ 强制两态互斥(Task 2.2)


class WriteToolHandler(Protocol):
    async def prepare(
        self, *, normalized_arguments: Mapping[str, Any]
    ) -> PreparedToolWrite: ...

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt: ...
```

`command` 只在当前进程的 prepare→execute 短链路中使用；恢复只读取已持久化的 `expected_state`。

**偏差 1（已接受）**：计划原文写 ``rejection: ToolExecutionReceipt | None``，但 ``prepare()`` 拿不到 ``tool_call_id``，无法构造该 Runtime 模型。改为不含 Runtime 标识的 ``PreparedWriteRejection``（``status`` / ``error_code`` / ``context`` / ``payload``），由 ToolExecutor 在执行边界补齐回执；``status != "FAILED"`` 的校验保留。

**偏差 3（已接受）**：拒绝路径**不伪造** ``expected_state``。Listing 不存在时本来就没有可信的 ``listing_id`` / ``currency`` / ``expected_version``，按成功路径补齐字段只会让形状看似统一而语义被削弱。因此 ``expected_state`` 在拒绝时为 ``None``，拒绝上下文只写入 ``Operation.last_receipt.rejection_context``（审计用途，不参与 Reconciliation）。两种准备的形状固定为：

- **可执行准备**：``command`` 与 ``expected_state`` 均非空、``rejection`` 为 ``None``；``expected_state`` 是完整、可回查的业务目标；
- **被拒绝准备**：``rejection`` 非空、``command`` 与 ``expected_state`` 均为 ``None``；``rejection.context`` 只含 PreToolUse 已固定的输入与策略版本。

两态互斥由 ``PreparedToolWrite.__post_init__`` 强制（Task 2.2）：混合状态或两项
皆缺都在构造时 ``ValueError``，而不是留给下游按字段是否为 None 猜分支。

价格模块的规范拒绝上下文形状与码值在 Task 6 收口。

**偏差 2（已修正）**：首版允许 ``NOT_APPLIED + NULL`` 直接重新绑定目标（理由见
Task 2.1）。评审指出该条件仅凭状态与空值推断"从未外部执行"，会把历史脏数据当成
安全状态；Task 2.2 改为要求 ``last_receipt`` 带受控的
``DETERMINISTIC_REJECTION`` 标记。

**四项决策的收口结论（Task 2.2）**

1. `NOT_APPLIED + NULL` 可重新绑定：**有条件接受**。同一 ToolCall 只有一行
   Operation 的推导成立，但绑定前提改为 `NOT_APPLIED + NULL +
   DETERMINISTIC_REJECTION`，不再仅凭状态与值推断；
2. `rejection_context` 继续放 `last_receipt`（不新增列）：**接受**，但明确它是
   "最新诊断快照"、会被后续结果覆盖，`request_fingerprint` 另写只追加的
   `TOOL_WRITE_REJECTED` 审计事件；
3. `TOOL_WRITE_REJECTED`：**签收**；
4. 所有复用路径强制能力一致：**设计接受，实现补齐**。确认预约即持久化完整
   `execution_profile`，`begin` 的 NULL 分支不再覆盖能力、统一比较。

- [ ] **Step 4: 让 `OperationService.begin` 接收并校验 expected_state**

签名改为：

```python
async def begin(
    self,
    tool_call_id: uuid.UUID,
    *,
    adapter_key: str,
    expected_state: Mapping[str, Any],
    supports_native_idempotency: bool = False,
    supports_reconciliation: bool = False,
    idempotency_key: str | None = None,
    request_fingerprint: str | None = None,
) -> BegunOperation:
```

首次创建写入 `expected_state=dict(expected_state)`；复用已有 Operation 时先收敛状态机，任一前提不满足即抛 `OperationConflictError`（fail closed）——禁止同一 operation_id 改变业务目标，也禁止改写执行能力（adapter_key / 原生幂等 / 回查）。状态机的完整规则与验收条件见 Task 2.1。

- [ ] **Step 5: 调整 ToolExecutor 顺序**

```python
prepared = await self._handler.prepare(
    normalized_arguments=normalized_arguments,
)
if prepared.rejection is not None:
    # 确定性拒绝:不进执行边界,直接落 NOT_APPLIED(见 Task 2.1/2.2)
    return await self._record_rejection(tool_call_id, prepared)
# 两态互斥由 PreparedToolWrite.__post_init__ 保证,此处只需收敛类型。
expected_target = prepared.expected_state
assert expected_target is not None
begun = await self._operations.begin(
    tool_call_id,
    adapter_key=self._execution_profile.adapter_key,
    expected_state=expected_target,
    supports_native_idempotency=self._execution_profile.supports_native_idempotency,
    supports_reconciliation=self._execution_profile.supports_reconciliation,
    request_fingerprint=prepared.request_fingerprint,
)
await self._checkpoint_if_configured()
receipt = await self._invoke_prepared_handler(
    tool_call_id=tool_call_id,
    operation_id=begun.operation_id,
    idempotency_key=begun.idempotency_key,
    prepared=prepared,
)
```

`prepared.rejection` 必须先于 `begin` 判断：先 begin 再判断会让一个"确定不会调用 Adapter"的写短暂落成 `EXECUTING` 并写下执行允许审计，在两次落库之间崩溃即触发无谓回查（Task 2.1 修正的正是这一点）。`expected_state` / `command` 的存在性检查在 Task 2.2 由
`PreparedToolWrite.__post_init__` 承担，执行器内不再重复判断。

输入解析错误应在 Schema/PreToolUse 阶段结束，不进入 `prepare()`；`prepare()` 只把确认后出现的资源变化表达为确定性 rejection。`execute_prepared()` 异常继续归一为 `UNKNOWN`。

- [ ] **Step 6: 将旧改价 Handler 迁移到新协议**

把 `_build_command()` 移入 `prepare()`，构造：

```python
expected_state = {
    "operation_type": "UPDATE_PRICE_LEGACY",
    "listing_id": str(command.listing_id),
    "store_id": str(command.store_id),
    "sku": command.sku,
    "target_price": str(command.target_price),
    "currency": command.currency,
    "expected_version": command.expected_version,
    "policy_version": self._policy_version,
}
PreparedToolWrite(
    command=command,
    expected_state=expected_state,
    request_fingerprint=hashlib.sha256(
        json.dumps(expected_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest(),
)
```

`execute_prepared()` 只接受 `prepared.command`，不得再次解析模型参数。

- [ ] **Step 7: 运行写执行和改价回归**

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/execution tests/agents/product_operations/test_price_flow.py tests/runtime/integration/test_crash_recovery.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add runtime/execution/tool_executor.py runtime/execution/operations.py agents/product_operations/tools/update_price.py composition/product_operations.py tests/runtime/execution/test_operations.py tests/runtime/execution/test_prepared_writes.py
git commit -m "refactor: persist prepared write intent before execution"
```

---

### Task 2.1: 收敛 begin 状态机并消除拒绝路径的不确定窗口

**Files:**
- Modify: `runtime/execution/operations.py`
- Modify: `runtime/execution/tool_executor.py`
- Modify: `agents/product_operations/tools/update_price.py`
- Modify: `tests/runtime/execution/test_prepared_writes.py`
- Modify: `tests/runtime/execution/test_operations.py`
- Modify: `tests/agents/product_operations/test_price_flow.py`

Task 2 的首版实现把两处风险留在了执行边界上，本任务专门收敛这两点，不改动
Task 2 已接受的偏差 1、3（Handler 两态契约与拒绝最小上下文）。

**背景（首版的两个缺陷）**

1. `expected_state IS NULL` 的复用分支只回填目标：既不把 `Operation.status`
   从 `PREPARED` 推进 `EXECUTING`，也不校验 `adapter_key` 与幂等/回查能力。
   若在 `begin()` 提交后、外部调用返回前崩溃，记录仍是 `PREPARED`，而
   `LeaseSweeper` 只在 `EXECUTING`/`UNKNOWN` 上回查，恢复方会把"可能已执行"
   误判为"尚未执行"→ 重复写。
2. `ToolExecutor` 对 `PreparedWriteRejection` 也先 `begin` → commit，再判断
   拒绝：明明已确定不调用 Adapter，却短暂落成"可能已执行"并写
   `TOOL_WRITE_ALLOWED`；两者之间崩溃即触发无谓回查。

**锁定的规则**

- **可执行写入**：`PREPARED → EXECUTING`，同一次提交里落库完整
  `expected_state` 与执行能力（`adapter_key` / `supports_native_idempotency` /
  `supports_reconciliation`），提交后才调用 Adapter；
- **确定性拒绝**：直接记录 `NOT_APPLIED`（`OperationService.record_rejected_preparation`），
  不进入 `EXECUTING`、不写 `TOOL_WRITE_ALLOWED`、不新增 EXECUTE Attempt，
  因而不产生外部执行的不确定窗口；
- **NULL `expected_state`**：只有在"可证明从未发起外部调用"的 Operation 上才允许
  绑定业务目标——`PREPARED`（确认前预约），或带 `DETERMINISTIC_REJECTION`
  标记的 `NOT_APPLIED`；其余状态（`EXECUTING` / `UNKNOWN` / `APPLIED` /
  `FAILED`）或缺少标记的 NULL 一律 fail closed；
- **已绑定目标**：`expected_state` 规范化 JSON 必须与已持久化值一致；
- **终态重入**：`APPLIED` / `UNKNOWN` / `FAILED` 不允许再次进入执行边界，
  只能走 Reconciliation。

**为何 NULL 可绑定不限于 `PREPARED`**：`operations` 表对
`tool_call_id` 有唯一约束，一个 ToolCall 只能有**一行** Operation；因此
"确认后 Listing 消失 → 拒绝"之后同一 ToolCall 被重新校验并再次执行时，
必须在这同一行上重新绑定目标，无法改由新行承担。

> **评审修正（Task 2.2）**：首版把可绑定条件写成"`status ∈ {PREPARED,
> NOT_APPLIED}`"，仅凭状态与空值推断"从未外部执行"——历史脏数据（适配器判定
> `NOT_APPLIED` 却丢失目标记录）会被当成安全状态。现改为要求 `last_receipt`
> 带受控标记 `stage=PREPARATION` / `kind=DETERMINISTIC_REJECTION`；该标记只由
> `record_rejected_preparation` 写入。执行能力也不再因为目标为空而被覆盖
> （见 Task 2.2）。

**验收条件**

- [x] `tests/runtime/execution/test_prepared_writes.py` 覆盖：真实确认路径的
  `PREPARED` 行被首次绑定（目标 + 执行能力同时落库）并推进 `EXECUTING`；
- [x] 覆盖 `EXECUTING` 但 `expected_state IS NULL` → `OperationConflictError`
  且 Adapter 零调用；
- [x] 覆盖执行能力不一致、终态重入 → `OperationConflictError`；
- [x] 覆盖拒绝路径：`Operation` 为 `NOT_APPLIED` 且 `expected_state IS NULL`、
  `last_receipt.rejection_context` 存在、无 EXECUTE Attempt、无
  `TOOL_WRITE_ALLOWED` 审计、`begun is None`；
- [x] `tests/agents/product_operations/test_price_flow.py` 覆盖：经真实组合根
  确认执行后，确认前的 `PREPARED` 行变为 `APPLIED` 并带上执行能力；
- [x] `tests/runtime/execution/test_operations.py` 的幂等 begin 用例在同一次
  提交里传出一致的执行能力（能力不一致本身即 fail closed）；
- [x] 全量门禁：`pytest` 全绿、`ruff check` 通过、`mypy runtime agents adapters
  composition apps eval` 通过。

**注意**：现有测试没有覆盖"确认后崩溃窗口"，因此"门禁全绿"本身不等于
Batch 1 架构验收通过；上述验收条件里与状态机相关的用例是补齐该缺口的部分。
其中"NULL 可绑定"与"能力一致性"两条在 Task 2.2 被收紧，以 Task 2.2 为准。

---

### Task 2.2: 重试语义显式化与能力落库收口

**Files:**
- Modify: `runtime/execution/operations.py`
- Modify: `runtime/execution/tool_executor.py`
- Modify: `runtime/execution/tool_result_processor.py`
- Modify: `runtime/orchestration/agent_loop.py`
- Modify: `agents/product_operations/tools/update_price.py`
- Modify: `adapters/commerce/protocol.py`、`adapters/commerce/mock_adapter.py`、
  `adapters/commerce/rest_adapter.py`、`composition/product_operations.py`
- Modify: `tests/runtime/execution/test_prepared_writes.py`、
  `tests/runtime/execution/test_operations.py`、
  `tests/runtime/execution/test_tool_result_processor.py`、
  `tests/runtime/orchestration/test_agent_loop.py`、
  `tests/agents/product_operations/test_price_flow.py`

**背景（首版留下的两个缺陷）**

1. **NULL 分支仍可静默改写执行能力**。确认预约（`_assign_operation_id`）时已经
   持有完整 `execution_profile`，却只保存 `adapter_key`；随后 `expected_state`
   为 NULL 的复用分支直接覆盖幂等/回查能力。于是"所有复用路径能力一致"并未真正
   成立。
2. **`NOT_APPLIED` 被无条件当成可重试**。`ToolResultProcessor` 把所有
   `NOT_APPLIED` 都标记为 `retry_allowed=True`，`OperationService` 又完全没有
   使用该字段区分 ToolCall 终态，`AgentLoop` 见到 `VALIDATING` 就再次创建
   `ADVANCE_RUN`。三者叠加构成完整的无限重排队条件：Runtime 把"确定失败"与
   "值得重试"画了等号。

**锁定的规则**

- `retry_allowed` 是**显式回执契约**，默认 false，从
  `ToolExecutionReceipt.retryable` 贯穿 `ProcessedToolResult` →
  `OperationResultRecord` → `RecordResultOutcome`；
- `NOT_APPLIED + retry_allowed=True`：ToolCall 回 `VALIDATING`（重走完整
  PreToolUse）；`NOT_APPLIED + retry_allowed=False`：ToolCall 直接 `FAILED`；
- `PreparedWriteRejection.retryable` 默认 false；`prepared.rejection` 与
  `command`/`expected_state` 由 `PreparedToolWrite.__post_init__` 强制两态互斥；
- 平台侧可重试性由 `adapters.commerce.protocol.RETRYABLE_REASON_CODES`
  单点定义（`PLATFORM_FAILURE`、`VERSION_CONFLICT`），随 `ExecutionReceipt.
  retryable` → `UpdatePriceWriteReceipt.retryable` → `ToolExecutionReceipt`
  透传；
- **确认预约即持久化完整执行能力**：`_assign_operation_id` 写入
  `supports_native_idempotency` / `supports_reconciliation`，`begin` 的全部
  复用路径（含 NULL 分支）统一比较能力，不再因 `expected_state is NULL` 覆盖；
- **受控来源标记**：`record_rejected_preparation` 写
  `last_receipt.stage="PREPARATION"` 与 `kind="DETERMINISTIC_REJECTION"`；
  只有 `NOT_APPLIED + NULL + DETERMINISTIC_REJECTION` 允许重新绑定目标；
- **只追加证据**：`request_fingerprint` 写进 `TOOL_WRITE_REJECTED` 审计事件
  （`last_receipt` 是会被后续结果覆盖的最新诊断快照，不作不可变证据）；
- **模型可见结果**：ToolResult 内容固定带 `operation_outcome` 与 `error_code`，
  否则终止重试后模型只能看到空 `{}`，无法决定下一步。

**范围说明**：Reconciliation 回查结论 `NOT_APPLIED` → `VALIDATING` 保持不变。
回查是把"结果未知"收敛为"确定未生效"的独立契约，其"确定未生效后可重走
PreToolUse"语义不经过 execute 回执，因此不受本次 `retry_allowed` 约束。

**验收条件**

- [x] `test_prepared_writes.py` 覆盖：`PreparedToolWrite` 两态互斥（混合状态与
  缺项都在构造时失败）、`rejection.retryable` 默认 false；
- [x] 覆盖 `NOT_APPLIED + NULL` 且无 `DETERMINISTIC_REJECTION` 标记 →
  `OperationConflictError`，Adapter 零调用、目标保持 NULL；
- [x] 覆盖带标记的 `NOT_APPLIED + NULL` 可重新绑定目标并推进 `EXECUTING`；
- [x] 覆盖 NULL 分支能力不一致 → `OperationConflictError` 且不覆盖已落库能力；
- [x] 覆盖拒绝路径 ToolCall 落 `FAILED`（默认不可重试）、`retry_allowed=True`
  时才回 `VALIDATING`；
- [x] 覆盖适配器确定失败的可重试/不可重试两条终态；
- [x] 覆盖拒绝的 `request_fingerprint` 出现在 `TOOL_WRITE_REJECTED` 审计 payload；
- [x] `test_tool_result_processor.py` 覆盖：`NOT_APPLIED` 缺省不可重试，仅
  `retryable=True` 回执才 `retry_allowed=True`；
- [x] `test_operations.py` 覆盖：`NOT_APPLIED` 的 ToolCall 终态由 `retry_allowed`
  决定（`VALIDATING` 或 `FAILED`）；
- [x] `test_agent_loop.py` 覆盖："确定性拒绝只执行一次"（ToolCall `FAILED`、
  Run 不重排队、prepare 恰好一次、Adapter 零调用）与"显式 retryable 才重排"
  （`VALIDATING` + `ADVANCE_RUN`）；
- [x] `test_price_flow.py` 覆盖：预约阶段即落库完整执行能力，执行后保持不变；
- [x] 全量门禁：`pytest` 全绿、`ruff check` 通过、`mypy runtime agents adapters
  composition apps eval` 通过。

**注意**：`retry_allowed=True` 的失败仍会重新排队；其上界由 Run 预算
（`max_tool_calls`、Run Deadline、单 Tool 超时）承担，本次不再新增重试计数。

---

### Task 3: 建立商品模块公共写契约和错误模型

**Files:**
- Create: `agents/product_operations/shared/__init__.py`
- Create: `agents/product_operations/shared/errors.py`
- Create: `agents/product_operations/shared/write_contracts.py`
- Create: `agents/product_operations/shared/reconciliation.py`
- Create: `tests/agents/product_operations/test_shared_contracts.py`

- [ ] **Step 1: 写公共契约失败测试**

```python
def test_unknown_receipt_cannot_claim_observed_state() -> None:
    with pytest.raises(ValueError, match="UNKNOWN receipt"):
        WriteReceipt(
            operation_id="op-1",
            idempotency_key="idem-1",
            status=WriteStatus.UNKNOWN,
            resource_version=2,
        )
```

- [ ] **Step 2: 实现规范错误和写回执**

`shared/errors.py` 固定以下错误码：

```python
AUTH_EXPIRED = "AUTH_EXPIRED"
RATE_LIMITED = "RATE_LIMITED"
CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
NOT_FOUND = "NOT_FOUND"
VALIDATION_FAILED = "VALIDATION_FAILED"
VERSION_CONFLICT = "VERSION_CONFLICT"
PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
AMBIGUOUS_LOCATION = "AMBIGUOUS_LOCATION"
INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
```

`shared/write_contracts.py`：

```python
class WriteStatus(str, enum.Enum):
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WriteReceipt:
    operation_id: str
    idempotency_key: str
    status: WriteStatus
    resource_version: int | None = None
    external_ref: str | None = None
    changed: bool | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is WriteStatus.UNKNOWN and any(
            value is not None
            for value in (self.resource_version, self.external_ref, self.changed)
        ):
            raise ValueError("UNKNOWN receipt cannot contain observed state")
```

- [ ] **Step 3: 实现商品 Reconciler Registry**

```python
class ProductOperationReconciler(Protocol):
    async def reconcile(self, expected_state: Mapping[str, Any]) -> ReconcileResult: ...


class OperationReconcilerRegistry:
    def __init__(self) -> None:
        self._by_operation_type: dict[str, ProductOperationReconciler] = {}

    def register(self, operation_type: str, reconciler: ProductOperationReconciler) -> None:
        if operation_type in self._by_operation_type:
            raise ValueError(f"duplicate reconciler: {operation_type}")
        self._by_operation_type[operation_type] = reconciler

    def require(self, operation_type: str) -> ProductOperationReconciler:
        try:
            return self._by_operation_type[operation_type]
        except KeyError as exc:
            raise ValueError(f"unregistered operation type: {operation_type}") from exc
```

- [ ] **Step 4: 运行测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/test_shared_contracts.py -q
git add agents/product_operations/shared tests/agents/product_operations/test_shared_contracts.py
git commit -m "feat: add product write and reconciliation contracts"
```

Expected: PASS，提交成功。

---

### Task 4: 增加库存位置模型并迁移 Listing 发布状态

**Files:**
- Create: `migrations/versions/0004_product_operations_p1.py`
- Modify: `agents/product_operations/domain/models.py`
- Modify: `agents/product_operations/persistence/models.py`
- Modify: `agents/product_operations/persistence/repository.py`
- Modify: `tests/persistence/test_migrations.py`
- Modify: `tests/agents/product_operations/test_domain_schema.py`
- Modify: `tests/fixtures/product_operations.py`

- [ ] **Step 1: 写 Schema 失败测试**

验证：

```python
assert set(ListingStatus) == {
    ListingStatus.PUBLISHED,
    ListingStatus.UNPUBLISHED,
}
assert seed.inventory_level.available_quantity == 20
assert seed.inventory_level.version == 1
```

并增加负库存约束测试，插入 `available_quantity=-1` 必须触发数据库约束错误。

- [ ] **Step 2: 编写 Alembic migration**

`upgrade()` 必须按以下顺序执行：

```python
op.drop_constraint("ck_listings_status", "listings", schema=SCHEMA, type_="check")
op.execute("UPDATE product_operations.listings SET status='PUBLISHED' WHERE status='ACTIVE'")
op.execute("UPDATE product_operations.listings SET status='UNPUBLISHED' WHERE status='INACTIVE'")
op.create_check_constraint(
    "ck_listings_status",
    "listings",
    "status IN ('PUBLISHED', 'UNPUBLISHED')",
    schema=SCHEMA,
)
```

然后创建：

```text
fulfillment_locations
inventory_levels
```

`inventory_levels` 包含 `CHECK (available_quantity >= 0)` 和 `(workspace_id, location_id, product_id)` 唯一约束。`downgrade()` 先删除新表，再把状态反向映射为 `ACTIVE/INACTIVE`。

- [ ] **Step 3: 增加 ORM 与领域模型**

```python
class ListingStatus(str, enum.Enum):
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"


class FulfillmentLocation(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "fulfillment_locations"
    workspace_id: Mapped[uuid.UUID]
    store_id: Mapped[uuid.UUID]
    external_location_ref: Mapped[str]
    name: Mapped[str]
    aliases: Mapped[list[str]]
    status: Mapped[StoreStatus]
    version: Mapped[int]


class InventoryLevel(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "inventory_levels"
    workspace_id: Mapped[uuid.UUID]
    location_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID]
    available_quantity: Mapped[int]
    version: Mapped[int]
```

- [ ] **Step 4: 增加 CAS Repository 方法**

```python
async def adjust_inventory(
    self,
    *,
    workspace_id: UUID,
    inventory_level_id: UUID,
    delta: int,
    expected_version: int,
) -> InventoryLevel | None:
```

SQL 条件必须同时包含 workspace、ID、version，并保证 `available_quantity + delta >= 0`。再增加以下方法：

```python
async def set_listing_publication_state(
    self,
    *,
    workspace_id: UUID,
    listing_id: UUID,
    target_state: ListingStatus,
    expected_version: int,
) -> Listing | None:
```

- [ ] **Step 5: 验证 migration 与模型**

```powershell
.venv\Scripts\python.exe -m pytest tests/persistence/test_migrations.py tests/agents/product_operations/test_domain_schema.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add migrations/versions/0004_product_operations_p1.py agents/product_operations/domain/models.py agents/product_operations/persistence/models.py agents/product_operations/persistence/repository.py tests/persistence/test_migrations.py tests/agents/product_operations/test_domain_schema.py tests/fixtures/product_operations.py
git commit -m "feat: add inventory levels and listing publication state"
```

---

### Task 5: 迁移 Catalog 与 Listing 查询能力

**Files:**
- Create: `agents/product_operations/catalog/__init__.py`
- Create: `agents/product_operations/catalog/models.py`
- Create: `agents/product_operations/catalog/service.py`
- Create: `agents/product_operations/catalog/tools.py`
- Create: `agents/product_operations/listing/__init__.py`
- Create: `agents/product_operations/listing/models.py`
- Create: `agents/product_operations/listing/service.py`
- Create: `agents/product_operations/listing/tools.py`
- Modify: `agents/product_operations/input/profiles.py`
- Create: `tests/agents/product_operations/catalog/test_catalog_tools.py`
- Create: `tests/agents/product_operations/listing/test_listing_query.py`

- [ ] **Step 1: 写新 Tool Schema 失败测试**

```python
def test_new_tools_expose_refs_not_internal_ids() -> None:
    assert set(SEARCH_PRODUCTS_DEFINITION.input_json_schema["properties"]) == {
        "query", "store_refs"
    }
    assert set(GET_LISTING_DEFINITION.input_json_schema["properties"]) == {
        "store_ref", "sku"
    }
```

- [ ] **Step 2: 实现 Catalog 模型与 Service**

```python
class SearchProductsInput(FrozenModel):
    query: str | None = None
    store_refs: tuple[str, ...] = ()


class CatalogService:
    async def search_products(
        self, *, actor: ActorContext, query: str | None, store_refs: Sequence[str]
    ) -> ProductSearchResult:
        store_ids = await self._stores.resolve_authorized_many(actor, store_refs)
        return await self._catalog.search(
            workspace_id=actor.workspace_id,
            query=query,
            store_ids=store_ids,
        )
```

实现沿用当前 Workspace 和授权店铺过滤，输出不包含成本价或外部平台凭证。

- [ ] **Step 3: 实现 `get_listing`**

```python
STABLE_KEY_GET_LISTING = "get_listing"

class GetListingInput(FrozenModel):
    store_ref: str
    sku: str

class ListingSnapshot(FrozenModel):
    store_ref: str
    sku: str
    title: str
    current_price: Decimal
    currency: str
    publication_state: Literal["PUBLISHED", "UNPUBLISHED", "UNKNOWN"]
    listing_version: int
    issues: tuple[str, ...] = ()
```

Handler 只接收模型引用，Service 使用授权的 `StoreReferenceResolver` 解析内部 UUID。

- [ ] **Step 4: 运行模块测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/catalog tests/agents/product_operations/listing/test_listing_query.py -q
git add agents/product_operations/catalog agents/product_operations/listing agents/product_operations/input/profiles.py tests/agents/product_operations/catalog tests/agents/product_operations/listing/test_listing_query.py
git commit -m "refactor: split catalog and listing query modules"
```

Expected: PASS，提交成功。

---

### Task 6: 迁移 Pricing 并发布准确的价格 Tool

**Files:**
- Create: `agents/product_operations/pricing/__init__.py`
- Create: `agents/product_operations/pricing/models.py`
- Create: `agents/product_operations/pricing/gateway.py`
- Create: `agents/product_operations/pricing/policy.py`
- Create: `agents/product_operations/pricing/service.py`
- Create: `agents/product_operations/pricing/tools.py`
- Create: `agents/product_operations/pricing/reconciliation.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Modify: `adapters/commerce/rest_adapter.py`
- Create: `tests/agents/product_operations/pricing/test_price_comparison.py`
- Create: `tests/agents/product_operations/pricing/test_set_listing_price.py`
- Create: `tests/agents/product_operations/pricing/test_price_reconciliation.py`

- [ ] **Step 1: 写价格比较和绝对设置失败测试**

```python
async def test_compare_listing_prices_returns_deterministic_summary(service) -> None:
    result = await service.compare_listing_prices(sku="SKU-1", store_refs=("a", "b"))
    assert result.lowest_price == Decimal("90.00")
    assert result.highest_price == Decimal("110.00")
    assert result.price_spread == Decimal("20.00")


def test_set_listing_price_has_only_business_inputs() -> None:
    assert set(SET_LISTING_PRICE_DEFINITION.input_json_schema["properties"]) == {
        "store_ref", "sku", "target_price"
    }
```

- [ ] **Step 2: 定义 Pricing Gateway 和命令**

```python
class PricingGateway(Protocol):
    async def get_price(self, query: PriceQuery) -> PriceSnapshot: ...
    async def set_price(self, command: SetListingPriceCommand) -> WriteReceipt: ...
    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult: ...
```

`SetListingPriceCommand` 必须包含 `operation_id`、可信内部资源、`target_price`、`currency`、`expected_version`；其 `idempotency_key` 由 Runtime 参数注入，不从 Tool input 读取。

- [ ] **Step 3: 实现确定性价格比较**

同币种时计算最低价、最高价和价差；存在多个币种时返回 `CURRENCY_MISMATCH`，不得由模型自行换算。

- [ ] **Step 4: 实现 `set_listing_price` prepare/execute**

`prepare()`：

```text
解析 store_ref → 读取 Listing → 执行 PricingPolicy.validate_execution
→ 构造 SetListingPriceCommand
→ 返回 expected_state(operation_type=SET_LISTING_PRICE)
```

`execute_prepared()` 只调用 `PricingGateway.set_price(command)` 并转换 `WriteReceipt`。

- [ ] **Step 5: 实现价格 Reconciler**

只使用 `Operation.expected_state` 构造 `ReconcilePriceCommand`；不得从最新对话、当前 Agent Prompt 或模型文本恢复目标价格。

- [ ] **Step 6: 迁移 Mock/REST Adapter 的价格接口**

`MockCommerceAdapter` 和 `RestCommerceAdapter` 实现 `PricingGateway`。REST 路径、安全主机、响应大小限制保持不变；旧 `execute/reconcile` 暂由兼容包装调用新方法。

- [ ] **Step 7: 运行价格全链路测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/pricing tests/adapters/commerce/test_mock_adapter.py tests/adapters/commerce/test_rest_adapter.py -q
git add agents/product_operations/pricing adapters/commerce/mock_adapter.py adapters/commerce/rest_adapter.py tests/agents/product_operations/pricing tests/adapters/commerce
git commit -m "refactor: establish pricing capability module"
```

Expected: PASS，提交成功。

---

### Task 7: 实现 Inventory 查询、调整和恢复

**Files:**
- Create: `agents/product_operations/inventory/__init__.py`
- Create: `agents/product_operations/inventory/models.py`
- Create: `agents/product_operations/inventory/gateway.py`
- Create: `agents/product_operations/inventory/policy.py`
- Create: `agents/product_operations/inventory/service.py`
- Create: `agents/product_operations/inventory/tools.py`
- Create: `agents/product_operations/inventory/reconciliation.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Create: `tests/agents/product_operations/inventory/test_inventory_tools.py`
- Create: `tests/agents/product_operations/inventory/test_inventory_policy.py`
- Create: `tests/agents/product_operations/inventory/test_inventory_recovery.py`

- [ ] **Step 1: 写位置解析和幂等失败测试**

```python
async def test_missing_location_is_resolved_only_when_one_active_location(service) -> None:
    result = await service.get_inventory_level(
        actor=actor, store_ref="旗舰店", sku="SKU-1", location_ref=None
    )
    assert result.location_ref == "默认仓"


async def test_multiple_locations_require_user_input(service) -> None:
    with pytest.raises(AmbiguousLocationError):
        await service.get_inventory_level(
            actor=actor, store_ref="旗舰店", sku="SKU-1", location_ref=None
        )


async def test_same_operation_does_not_apply_delta_twice(adapter, command) -> None:
    await adapter.adjust_inventory(command)
    await adapter.adjust_inventory(command)
    assert await adapter.count_logical_writes(command.operation_id) == 1
```

- [ ] **Step 2: 定义 Tool 和 Gateway**

```python
STABLE_KEY_GET_INVENTORY_LEVEL = "get_inventory_level"
STABLE_KEY_ADJUST_INVENTORY_QUANTITY = "adjust_inventory_quantity"

class InventoryGateway(Protocol):
    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot: ...
    async def adjust_inventory(self, command: AdjustInventoryCommand) -> WriteReceipt: ...
    async def reconcile_inventory(self, command: ReconcileInventoryCommand) -> ReconcileResult: ...
```

- [ ] **Step 3: 实现 InventoryPolicy**

规则必须覆盖：`delta != 0`、调整后数量非负、位置属于店铺、原因属于受控枚举、资源版本未变化。原因枚举固定为：

```python
class InventoryAdjustmentReason(str, enum.Enum):
    MANUAL_CORRECTION = "MANUAL_CORRECTION"
    RECEIPT = "RECEIPT"
    DAMAGE = "DAMAGE"
    RETURN = "RETURN"
```

- [ ] **Step 4: 实现 prepare/execute 和 expected_state**

库存 `expected_state` 必须包含 `quantity_before`、`delta`、`expected_quantity_after`、`expected_version`、位置和原因。Adapter 回执丢失后禁止直接重放 `delta`。

- [ ] **Step 5: 实现 Mock Adapter 与 Reconciler**

Mock Adapter 按 operation_id 去重，用 CAS 更新数量。Reconciler 同时验证操作记录和预期调整后数量；无法区分并发变化时返回 `UNKNOWN`，不得猜测 `APPLIED`。

- [ ] **Step 6: 运行库存测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/inventory -q
git add agents/product_operations/inventory adapters/commerce/mock_adapter.py tests/agents/product_operations/inventory
git commit -m "feat: add inventory adjustment capability"
```

Expected: PASS，提交成功。

---

### Task 8: 实现 Listing 发布、取消发布和恢复

**Files:**
- Create: `agents/product_operations/listing/gateway.py`
- Create: `agents/product_operations/listing/policy.py`
- Create: `agents/product_operations/listing/reconciliation.py`
- Modify: `agents/product_operations/listing/models.py`
- Modify: `agents/product_operations/listing/service.py`
- Modify: `agents/product_operations/listing/tools.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Create: `tests/agents/product_operations/listing/test_publication_tools.py`
- Create: `tests/agents/product_operations/listing/test_publication_recovery.py`

- [ ] **Step 1: 写发布状态幂等失败测试**

```python
async def test_publish_already_published_listing_is_successful_noop(service) -> None:
    result = await service.publish_listing(store_ref="旗舰店", sku="SKU-1")
    assert result.status == "APPLIED"
    assert result.changed is False


async def test_unpublish_does_not_zero_inventory(service, repository, seed) -> None:
    await service.unpublish_listing(store_ref="旗舰店", sku="SKU-1", reason="MANUAL")
    inventory = await repository.get_inventory_level(
        workspace_id=seed.workspace.id,
        location_id=seed.fulfillment_location.id,
        product_id=seed.product.id,
    )
    assert inventory.available_quantity == 20
```

- [ ] **Step 2: 定义 ListingGateway**

```python
class ListingGateway(Protocol):
    async def get_listing(self, query: ListingQuery) -> ListingSnapshot: ...
    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt: ...
    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt: ...
    async def reconcile_listing(self, command: ReconcileListingCommand) -> ReconcileResult: ...
```

- [ ] **Step 3: 实现状态规则**

允许 `UNPUBLISHED → PUBLISHED`、`PUBLISHED → UNPUBLISHED` 和目标状态相同的幂等 no-op。平台能力缺失返回 `CAPABILITY_UNSUPPORTED`；不得回退为删除商品或库存清零。

- [ ] **Step 4: 实现 prepare/execute 和 Reconciler**

`expected_state` 保存 listing_id、目标发布状态、expected_version 和 reason。Reconciler 读取平台刊登状态并只返回 Runtime 的三态结论。

- [ ] **Step 5: 运行 Listing 测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/listing -q
git add agents/product_operations/listing adapters/commerce/mock_adapter.py tests/agents/product_operations/listing
git commit -m "feat: add listing publication capability"
```

Expected: PASS，提交成功。

---

### Task 9: 建立 Adapter 能力契约测试

**Files:**
- Create: `tests/adapters/commerce/contracts.py`
- Modify: `tests/adapters/commerce/test_mock_adapter.py`
- Modify: `tests/adapters/commerce/test_rest_adapter.py`
- Modify: `adapters/commerce/rest_adapter.py`
- Modify: `adapters/commerce/__init__.py`

- [ ] **Step 1: 提取共享契约测试**

`contracts.py` 提供三个可复用协程：`assert_price_gateway_contract`、`assert_inventory_gateway_contract`、`assert_listing_gateway_contract`。每个协程接收 Gateway 和对应 Fixture，并顺序执行成功写入、旧版本冲突、同 `operation_id` 重复提交、写后丢响应、按该 `operation_id` 回查五个断言；每一步都断言最终资源状态和逻辑写次数。

- [ ] **Step 2: Mock Adapter 跑三套契约**

```python
async def test_mock_adapter_satisfies_all_supported_contracts(mock_adapter, fixture) -> None:
    await assert_price_gateway_contract(mock_adapter, fixture.price)
    await assert_inventory_gateway_contract(mock_adapter, fixture.inventory)
    await assert_listing_gateway_contract(mock_adapter, fixture.listing)
```

- [ ] **Step 3: REST Adapter 明确能力范围**

P1 的 REST Adapter 只声明 `pricing` 能力。库存和 Listing 方法若被误调用，返回结构化 `CAPABILITY_UNSUPPORTED`，不构造猜测性 URL。不要为了契约测试虚构第三方库存或刊登端点。

- [ ] **Step 4: 运行 Adapter 测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/adapters/commerce -q
git add adapters/commerce tests/adapters/commerce
git commit -m "test: add commerce gateway contract suites"
```

Expected: PASS，提交成功。

---

### Task 10: 将 PricePolicyConfig 接入正式 Workspace 配置

**Files:**
- Create: `agents/product_operations/pricing/configuration.py`
- Modify: `composition/provisioning/product_operations.py`
- Modify: `composition/product_operations.py`
- Modify: `runtime/capabilities/run_factory.py`
- Create: `tests/agents/product_operations/pricing/test_configuration.py`
- Modify: `tests/composition/test_product_operations_provisioning.py`

- [ ] **Step 1: 写“缺少已发布价格策略即装配失败”测试**

```python
async def test_build_rejects_missing_published_pricing_policy(
    engine, capabilities, workspace_id, actor_id, membership_id
) -> None:
    with pytest.raises(AssemblyError, match="PRICING_POLICY_NOT_PUBLISHED"):
        await build_product_application(
            engine=engine,
            capabilities=capabilities,
            backend=ScriptedBackend([]),
            workspace_id=workspace_id,
            actor_id=actor_id,
            membership_id=membership_id,
        )
```

- [ ] **Step 2: 实现价格策略发布和加载**

stable key 固定为 `product-pricing`。Provisioner 接收显式 `PricePolicyConfig`，发布到已有 `BusinessPolicy`/`BusinessPolicyVersion`，并把精确 `business_policy_version_id` 放入能力快照。

```python
@dataclass(frozen=True)
class PublishedPricingPolicy:
    version_id: UUID
    config: PricePolicyConfig
    config_hash: str
```

- [ ] **Step 3: Run 固定 PolicySnapshot**

创建 Run 时把 P1 已解析的价格策略版本写入 `RunPolicySnapshot`。执行仍读取当前权限和资源状态，但确认指纹与 Operation 记录已验证的 policy version/hash。

- [ ] **Step 4: 删除正式装配路径中的 `price_config` 常量注入**

测试/Eval 可以通过显式 Provisioner fixture 创建策略版本；正式 `build_product_application` 只消费已发布快照。

- [ ] **Step 5: 运行配置测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/pricing/test_configuration.py tests/composition/test_product_operations_provisioning.py -q
git add agents/product_operations/pricing/configuration.py composition/provisioning/product_operations.py composition/product_operations.py runtime/capabilities/run_factory.py tests/agents/product_operations/pricing/test_configuration.py tests/composition/test_product_operations_provisioning.py
git commit -m "feat: publish workspace pricing policy configuration"
```

Expected: PASS，提交成功。

---

### Task 11: 发布 ProductOperations Agent 与兼容版本

**Files:**
- Modify: `agents/product_operations/agent.py`
- Modify: `agents/product_operations/prompt.py`
- Modify: `agents/product_operations/skill.py`
- Modify: `composition/provisioning/product_operations.py`
- Modify: `runtime/workspace/agent_binding.py`
- Modify: `tests/composition/test_product_operations_provisioning.py`
- Modify: `tests/agents/product_operations/test_agent_assembly.py`

- [ ] **Step 1: 写 stable key 和版本可见性失败测试**

```python
def test_new_agent_exposes_only_p1_business_tools() -> None:
    assert AGENT_KEY == "product-operations"
    assert PRODUCT_AGENT_DEFINITION.required_tool_keys == frozenset({
        "load_skill",
        "search_products",
        "get_listing",
        "compare_listing_prices",
        "set_listing_price",
        "get_inventory_level",
        "adjust_inventory_quantity",
        "publish_listing",
        "unpublish_listing",
    })


async def test_legacy_run_resolves_old_tool_version(application, legacy_run) -> None:
    pin = application.tool_pins.by_version_id(legacy_run.update_price_version_id)
    assert pin is not None
    assert pin.stable_key == "update_price"
```

- [ ] **Step 2: 发布新 Agent 定义**

```python
AGENT_KEY: Final = "product-operations"
AGENT_VERSION: Final = 1
P1_TOOL_KEYS: Final = frozenset({
    "search_products",
    "get_listing",
    "compare_listing_prices",
    "set_listing_price",
    "get_inventory_level",
    "adjust_inventory_quantity",
    "publish_listing",
    "unpublish_listing",
})
P1_LOOP_LIMITS: Final = {
    "max_model_rounds": 12,
    "max_tool_calls": 12,
    "tool_timeout_seconds": 60,
    "run_deadline_seconds": 3600,
    "backend_retries": 1,
    "reconcile_budget": 3,
}
PRODUCT_AGENT_DEFINITION = AgentDefinition(
    name="ProductOperationsAgent",
    system_prompt_version="product-operations-prompt-v1",
    required_backend_capabilities=frozenset({"streaming", "tool_calling"}),
    required_skill_keys=frozenset({SKILL_KEY}),
    required_tool_keys=P1_TOOL_KEYS | {LOAD_SKILL_KEY},
    required_source_keys=frozenset(),
    loop_limits=P1_LOOP_LIMITS,
)
```

保留旧定义为 `LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION`，只用于兼容版本注册。

- [ ] **Step 3: 更新 Prompt**

Prompt 使用新 Tool 名称和四个能力范围，不写价格阈值、权限实现、幂等或恢复内部细节。仍保留当前第一版强制 `load_skill` 行为，因为 ADR-001 尚未实施。

- [ ] **Step 4: 发布兼容性 SkillVersion**

Skill stable key 仍为 `product_maintenance`，版本递增；只替换旧 Tool 名称并补充库存、上下架的任务方法。不得在本任务删除 `required_tool_keys`、修改 `required_skill_keys` 或移动 `load_skill`。

- [ ] **Step 5: Provisioner 同时返回 current 与 compatibility 版本**

```python
@dataclass(frozen=True)
class ProductOperationsCapabilities:
    agent_id: UUID
    agent_version_id: UUID
    skill_id: UUID
    skill_version_id: UUID
    tool_definition_ids: Mapping[str, UUID]
    tool_version_ids: Mapping[str, UUID]
    compatibility_agent_version_ids: tuple[UUID, ...]
    compatibility_skill_version_ids: tuple[UUID, ...]
    compatibility_tool_version_ids: Mapping[str, tuple[UUID, ...]]
    model_config_version_id: UUID
    pricing_policy_version_id: UUID
```

Workspace 新绑定切换到 `product-operations`；旧版本行不删除。

- [ ] **Step 6: 运行发布与恢复测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/composition/test_product_operations_provisioning.py tests/agents/product_operations/test_agent_assembly.py -q
git add agents/product_operations/agent.py agents/product_operations/prompt.py agents/product_operations/skill.py composition/provisioning/product_operations.py runtime/workspace/agent_binding.py tests/composition/test_product_operations_provisioning.py tests/agents/product_operations/test_agent_assembly.py
git commit -m "feat: publish product operations agent capabilities"
```

Expected: PASS，提交成功。

---

### Task 12: 拆小组合根并接入模块化恢复

**Files:**
- Create: `composition/product_operations_bindings.py`
- Create: `composition/product_operations_recovery.py`
- Modify: `composition/product_operations.py`
- Modify: `tests/architecture/test_dependency_boundaries.py`
- Create: `tests/composition/test_product_operations_bindings.py`
- Create: `tests/composition/test_product_operations_recovery.py`

- [ ] **Step 1: 写组合根边界失败测试**

```python
def test_composition_has_no_domain_dispatch_or_policy_classes() -> None:
    source = Path("composition/product_operations.py").read_text(encoding="utf-8")
    assert "class _PriceBusinessPolicy" not in source
    assert "if key == STABLE_KEY" not in source
    assert "ReconciliationCommand(" not in source
```

- [ ] **Step 2: 建立声明式 Binding 工厂**

`product_operations_bindings.py` 暴露：

```python
def build_product_tool_bindings(deps: ProductOperationsDependencies) -> tuple[VersionedToolBinding, ...]:
    groups = (
        build_catalog_bindings(deps.catalog),
        build_pricing_bindings(deps.pricing),
        build_inventory_bindings(deps.inventory),
        build_listing_bindings(deps.listing),
        build_runtime_control_bindings(deps.runtime_control),
        build_legacy_bindings(deps.compatibility),
    )
    return tuple(binding for group in groups for binding in group)
```

返回前展平并校验 version_id 唯一；不得按 stable key 运行时分支。

- [ ] **Step 3: 建立恢复应用服务**

`product_operations_recovery.py` 从 Operation 读取 `expected_state.operation_type`，经 `OperationReconcilerRegistry` 查找 Reconciler，再把结果交给 `Runtime ReconciliationService.apply_result()`。

- [ ] **Step 4: 收缩 `composition/product_operations.py`**

最终只保留：读取已发布快照、创建 Adapter/Service/Registry/Runtime、连接依赖、返回 `ProductOperationsApplication`。删除价格命令转换、读 Tool 分发和领域回查判断。

- [ ] **Step 5: 运行组合与架构测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/composition tests/architecture/test_dependency_boundaries.py -q
git add composition/product_operations.py composition/product_operations_bindings.py composition/product_operations_recovery.py tests/composition tests/architecture/test_dependency_boundaries.py
git commit -m "refactor: reduce product operations composition root"
```

Expected: PASS，提交成功。

---

### Task 13: 建立 EvalCapabilityManifest 并迁移评测协议

**Files:**
- Create: `eval/capability_manifest.py`
- Modify: `eval/config.py`
- Modify: `eval/fixtures.py`
- Modify: `eval/product_suite.py`
- Modify: `eval/reporting/json_report.py`
- Modify: `eval/reporting/markdown_report.py`
- Modify: `eval/datasets/product_operations/*.yaml`
- Modify: `eval/datasets/product_operations/manifest.json`
- Create: `tests/eval/test_capability_manifest.py`
- Modify: `tests/eval/test_runner_isolation.py`
- Modify: `tests/eval/test_reporting.py`

- [ ] **Step 1: 写版本冻结失败测试**

```python
async def test_eval_manifest_is_frozen_before_cases_run(eval_fixture) -> None:
    manifest = await build_eval_capability_manifest(eval_fixture.capabilities)
    await eval_fixture.publish_new_tool_version("set_listing_price")
    assert await build_case_config(manifest).tool_version_ids == manifest.tool_version_ids
```

- [ ] **Step 2: 定义 EvalCapabilityManifest**

```python
class EvalCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_version_id: UUID
    prompt_version: str
    skill_version_ids: dict[str, UUID]
    tool_version_ids: dict[str, UUID]
    model_config_version_id: UUID
    model_snapshot: str
    policy_version_ids: dict[str, UUID]
    dataset_hash: str
    scorer_version: str
```

- [ ] **Step 3: 删除 Eval 中复制的能力版本常量**

删除 `_FIXED_AGENT_VERSION`、`_FIXED_PROMPT_VERSION`、`_FIXED_SKILL_VERSIONS`、`_FIXED_TOOL_VERSIONS`。Provider 配置仍只负责模型连接和固定采样参数；能力版本来自 Provisioner 返回的精确快照。

- [ ] **Step 4: 更新数据集 Tool 名称但不更新成绩**

将用例中的旧 Tool 断言迁移到八个新 stable key；库存和刊登各增加核心正常任务、安全拒绝和恢复案例。更新 dataset hash。不要在本任务生成或手工修改任务完成率。

- [ ] **Step 5: 报告写入 Manifest**

JSON 和 Markdown 报告必须记录完整 manifest 与协议指纹，确保两次成绩可以追溯到同一能力快照。

- [ ] **Step 6: 运行 Eval 单元测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/eval -q
git add eval tests/eval
git commit -m "refactor: freeze eval capability manifest"
```

Expected: PASS，提交成功。

---

### Task 14: 完成端到端闭环、文档收口和全量验收

**Files:**
- Modify: `tests/apps/api/test_e2e_product_flow.py`
- Modify: `tests/apps/worker/test_worker_lifecycle.py`
- Create: `tests/agents/product_operations/test_p1_safety_matrix.py`
- Modify: `docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md`
- Modify: `README.md`

- [ ] **Step 1: 增加四条端到端主路径**

覆盖：

```text
搜索商品 → 比价 → 设置 Listing 价格 → 回查
读取库存 → 增量调整 → 回查
读取 Listing → 发布 → 回查
读取 Listing → 取消发布 → 回查
```

每条写路径同时验证 `AUTO_EXECUTE` 与 `REQUIRE_CONFIRMATION`，确认后必须重新校验权限、Policy 和资源版本。

- [ ] **Step 2: 增加安全矩阵**

对四个写 Tool 参数化验证：无写权限、跨店铺、缺少关键范围、确认后版本变化、响应丢失、重复 operation_id、Adapter 不支持能力。

- [ ] **Step 3: 运行针对性测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations tests/adapters/commerce tests/composition tests/apps/api/test_e2e_product_flow.py tests/apps/worker/test_worker_lifecycle.py tests/runtime/integration/test_crash_recovery.py -q
```

Expected: PASS。

- [ ] **Step 4: 运行全量质量门禁**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy runtime agents adapters composition apps eval
```

Expected:

```text
pytest: all passed
ruff: All checks passed!
mypy: Success: no issues found
```

- [ ] **Step 5: 检查旧命名残留**

```powershell
rg -n "ProductMaintenanceAgent|product-maintenance|get_product_snapshot|update_price" agents composition apps eval README.md docs/架构
```

Expected: 命中只允许出现在明确标注的 legacy compatibility、历史迁移说明和 ADR 当前事实段；新 Agent、Prompt、Eval 数据集和普通装配路径不得命中。

- [ ] **Step 6: 更新架构事实**

只有全部门禁通过后，才将 ADR-002 状态改为“P0/P1 已实施，P2 待实施”，记录实际提交号、最终测试数量和验证命令。README 只描述已实现能力，不宣称接入真实第三方平台。

- [ ] **Step 7: 提交验收文档与测试**

```powershell
git add tests/apps/api/test_e2e_product_flow.py tests/apps/worker/test_worker_lifecycle.py tests/agents/product_operations/test_p1_safety_matrix.py docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md README.md
git commit -m "test: close product operations p1"
```

---

## 最终验收清单

- [ ] 新 Run 使用 `product-operations` 和八个冻结 Tool stable key；
- [ ] 旧 Run 可以按旧 Version ID 恢复，旧 Tool 不向新 Agent 暴露；
- [ ] ToolBinding 按 ToolVersion ID 绑定；
- [ ] Operation.expected_state 在外部写入前持久化；
- [ ] Pricing、Inventory、Listing 写操作都有 Reconciler；
- [ ] 增量库存响应丢失后不会重复扣减或增加；
- [ ] `unpublish_listing` 不删除 Product、Listing 或库存；
- [ ] PricePolicyConfig 来自已发布 Workspace 配置；
- [ ] Eval 报告使用冻结的真实能力版本；
- [ ] Runtime 不导入 `agents.product_operations` 或 `adapters.commerce`；
- [ ] 组合根不包含领域规则和按 Tool 业务分支；
- [ ] Skill 只完成兼容版本更新，ADR-001 目标模型仍标记为未实施；
- [ ] 全量 pytest、ruff、mypy 通过；
- [ ] P1 完成后重新运行真实模型评测，再决定是否更新评测报告。
