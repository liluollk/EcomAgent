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

## 剩余阶段连续执行协议（2026-10-14）

本轮从提交 `a689765` 开始，只执行 **Task 8.2 与 Task 9–14**。Task 1–8.1
均视为已完成历史，不重新实现、不改写既有提交。执行者必须使用
`superpowers:executing-plans` 连续完成剩余任务，不在 Task、Step 或批次之间等待
人工签收；只有下列情况才允许暂停并请求用户决策：

1. 需要改变已批准的模块化单体边界、Runtime/业务边界或 ADR-001 Skill 目标模型；
2. 需要删除历史 `AgentVersion` / `SkillVersion` / `ToolVersion`、执行破坏性迁移，
   或覆盖用户未提交改动；
3. 真实模型评测缺少外部凭据或 Provider 不可用。此类外部阻塞不得伪装成代码失败，
   也不得阻止确定性代码、测试和文档完成。

连续执行规则：

- 严格按 `Task 8.2 → 9 → 10 → 11 → 12 → 13 → 14` 顺序执行；后续任务可以
  消费前序任务公开契约，不得绕过前序任务临时复制实现。
- 每个 Task 仍遵循 TDD：新增测试先证明旧实现失败，再写最小实现；如果测试在旧实现
  上已通过，必须重写测试，使它能够锁定本任务真正新增的不变量。
- 每个 Task 完成后运行该 Task 的聚焦门禁并单独提交，然后直接进入下一 Task；不推送、
  不创建 PR，不把 `.workbuddy/`、`docs/审计/`、`reports/` 等既有未跟踪目录纳入提交。
- 发现局部实现偏差时，执行者可在不改变架构口径的前提下自行修正，并把“计划偏差、
  原因、验证证据”记入最终报告；不得为了赶进度降低 fail-closed、权限、幂等、恢复或
  Workspace 隔离标准。
- Task 14 之前不得手工修改评测成绩、简历数字或首版报告中的历史结果。真实模型评测
  只能基于最终冻结的 `EvalCapabilityManifest` 运行，并将新结果写入新的报告文件。
- 最终只进行一次统一交付：提交列表、最终门禁、真实模型评测状态、计划偏差和剩余风险。
  中途无需输出长篇阶段报告。

### 连续执行批次

| 批次 | 任务 | 目标 | 批次退出条件 |
|---|---|---|---|
| A | Task 8.2–9 | 锁定幂等命名空间并建立 Adapter 契约 | Commerce Adapter 聚焦测试通过 |
| B | Task 10–12 | 发布 Workspace Policy、新 Agent 版本并收缩组合根 | Composition + Architecture 测试通过 |
| C | Task 13–14 | 冻结 Eval 能力快照，完成 E2E、安全矩阵与文档 | 全量 pytest、ruff、mypy 通过 |

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
  > **Task 2.3 修正**：该集合已清空（两项都不是可重试语义），
  > `PreparedWriteRejection.retryable` 已删除。以 Task 2.3 为准。
- **确认预约即持久化完整执行能力**：`_assign_operation_id` 写入
  `supports_native_idempotency` / `supports_reconciliation`，`begin` 的全部
  复用路径（含 NULL 分支）统一比较能力，不再因 `expected_state is NULL` 覆盖；
- **受控来源标记**：`record_rejected_preparation` 写
  `last_receipt.stage="PREPARATION"` 与 `kind="DETERMINISTIC_REJECTION"`；
  只有 `NOT_APPLIED + NULL + DETERMINISTIC_REJECTION` 允许重新绑定目标；
  > **Task 2.3 修正**：还需**不存在 EXECUTE Attempt**；且该标记不再覆盖已有
  > 执行回执的 `NOT_APPLIED`。
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
  > **Task 2.3 修正**：拒绝不再有可重试开关，一律落 `FAILED`；可重试性只属于
  > **执行回执**（`ToolExecutionReceipt.retryable`）。
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

### Task 2.3: 移除错误的重试语义并保护执行来源

**Files:**
- Modify: `adapters/commerce/protocol.py`
- Modify: `runtime/execution/operations.py`
- Modify: `runtime/execution/tool_executor.py`
- Modify: `eval/datasets/recovery/faults.yaml`、`eval/recovery_suite.py`
- Modify: `tests/adapters/commerce/test_mock_adapter.py`、
  `tests/runtime/execution/test_prepared_writes.py`、
  `tests/runtime/orchestration/test_agent_loop.py`、
  `tests/eval/test_recovery_suite.py`

**背景（Task 2.2 三处决策的复评结论）**

Task 2.2 的 `a283598` 主链路正确，但三项决策里只有"拒绝上下文放
`last_receipt`"可保留，另两处需要收紧——不为了保住现有 Eval 预期而保留错误的
**生产**语义：

1. **可重试集合与真实状态机冲突**。`PLATFORM_FAILURE` 由
   `permanent_failure`（持续不可用）产生，包装成"暂时不可用"只是让 Eval 通过，
   会导致持续重排；`VERSION_CONFLICT` 也无法在同一 `operation_id` 下更新
   `expected_version`，且平台已按该 `operation_id` 记录失败回执，重试只会回放
   旧结论。两者都会把 ToolCall 带入**没有成功出口的重排循环**。
2. **最新拒绝会覆盖外部执行来源**。允许已有 `NOT_APPLIED` Operation 记录
   prepare 拒绝，并把 `last_receipt` 覆盖为
   `PREPARATION/DETERMINISTIC_REJECTION`——若该行此前已有 EXECUTE Attempt，
   这个标记就不再能证明"从未外部执行"，还能据此获得空目标重绑资格。
3. **`PreparedWriteRejection.retryable` 是危险逃生口**。商品域没有调用方，且
   "确定性 prepare 拒绝"与"自动重试"语义相冲突，保留只会给后续 Inventory /
   Pricing 留下误用空间。

**锁定的规则**

- `RETRYABLE_REASON_CODES` **当前为空**：任何确定失败都落 ToolCall 终态。将来若
  需自动重试，新增独立原因码（如 `PLATFORM_UNAVAILABLE` / `RATE_LIMITED`）并
  同时引入**持久化重试预算与退避**，而不是复用"重试必然同结果"的码值；
- **删除 `PreparedWriteRejection.retryable`**：确定性拒绝让当前 ToolCall 终止、
  把错误返回模型；暂时性基础设施故障走独立的 retryable failure 契约；资源版本
  变化应产生新的 ToolCall/Operation，不得复用旧确认与旧目标；
- **不覆盖执行来源**：`record_rejected_preparation` 仅对
  `PREPARED`／上一次也是 prepare 拒绝的行改写 `last_receipt` 标记；已有执行历史
  （存在 EXECUTE Attempt 或 `last_receipt.stage=EXECUTION`）的 `NOT_APPLIED`
  保留原执行回执与完整 `expected_state`，新拒绝只写追加式
  `TOOL_WRITE_REJECTED` 审计；
- **空目标重绑双重前提**：`NOT_APPLIED + NULL + DETERMINISTIC_REJECTION` 之外，
  还必须**不存在 EXECUTE Attempt**；因此不支持把拒绝限制为"仅 `PREPARED`"——同一
  ToolCall 只有一行 Operation，重新校验后必须能在同一行重绑——但必须保护执行历史；
- **Eval 口径**：`platform-temporarily-unavailable` 改为
  `platform-persistent-failure`（持续故障不是"暂时不可用"，不能用 `permanent_failure`
  模拟临时故障），期望改为确定 `NOT_APPLIED` + ToolCall `FAILED` 终态 + 零写 +
  不产生 UNKNOWN，Run 由模型收尾 COMPLETED；
- **Reconciliation 的 `NOT_APPLIED → VALIDATING`** 作为独立契约保持不变；它仍
  缺少跨轮次写重试预算，不阻塞 Task 4/5，但 **Task 6 的 Pricing 恢复验收必须补上
  该边界**。

**验收条件**

- [x] `test_prepared_writes.py` 覆盖：`PreparedWriteRejection` 无可重试字段；
- [x] 覆盖"已到过执行边界的 `NOT_APPLIED` 不被后续拒绝改写来源标记"（保留
  EXECUTION 回执与 `expected_state`，新拒绝只追加审计）；
- [x] 覆盖"带标记但存在 EXECUTE Attempt 的 `NOT_APPLIED + NULL` → fail closed，
  Adapter 零调用、目标保持 NULL"；
- [x] `test_agent_loop.py` 覆盖：`VERSION_CONFLICT` 不重排、ToolCall `FAILED`、
  失败返回模型（prepare 恰好一次、Adapter 一次）；
- [x] `test_mock_adapter.py` 覆盖：`PLATFORM_FAILURE` / `VERSION_CONFLICT` 回执
  `retryable is False`，`is_retryable_reason_code` 对全部确定失败码返回 False；
- [x] `test_recovery_suite.py` / `faults.yaml` 使用新用例 id 与期望并通过；
- [x] 全量门禁：`pytest` 875 passed、`ruff check .` 通过、`mypy runtime agents
  adapters composition apps eval` 通过。

**注意**：现有 Eval 预期为迁就错误语义而写成"回执显式声明 retryable → 回
VALIDATING"，Task 2.3 后该预期不再成立；用例改为验证**持续故障的确定语义**。

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
- Modify: `agents/product_operations/tools/search_products.py`
- Modify: `agents/product_operations/policy/price_policy.py`
- Modify: `tests/persistence/test_migrations.py`
- Modify: `tests/agents/product_operations/test_domain_schema.py`、
  `tests/agents/product_operations/test_read_tools.py`、
  `tests/agents/product_operations/test_price_policy.py`、
  `tests/agents/product_operations/conftest.py`
- Modify: `tests/fixtures/product_operations.py`
- Modify: `tests/apps/api/test_e2e_product_flow.py`（清理顺序）
- Modify: `eval/fixtures.py`、`eval/recovery_suite.py`、`eval/runtime_suite.py`
- Modify: `tests/eval/conftest.py`、`tests/eval/test_runner_isolation.py`、
  `tests/eval/test_scorers.py`
- Modify: `eval/datasets/product_operations/*.yaml`、
  `eval/datasets/product_operations/manifest.json`

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

**实现补充（落地时发现，超出原文件清单）**

`ListingStatus` 从 `ACTIVE/INACTIVE` 迁移为 `PUBLISHED/UNPUBLISHED` 会扩散到所有
引用该枚举的读写路径与评测数据集，以下一并修改（否则 `pytest` 门禁无法保持绿）：

- `agents/product_operations/tools/search_products.py`、`policy/price_policy.py`：
  兼容实现里的 `ListingStatus.ACTIVE` 比较改为 `PUBLISHED`（行为不变：已发布
  Listing 才是可读/可写对象）；legacy 错误码 `LISTING_NOT_ACTIVE` 保留不改，属
  兼容契约，由 Task 6 的 `pricing/policy.py` 取代；
- `tests/agents/product_operations/{test_read_tools,test_price_policy,conftest}.py`：
  断言与清理语句同步；清理顺序补 `inventory_levels`、`fulfillment_locations`
  （新表对 `products`/`stores` 有外键，原顺序会触发 FK 违约）；
- `eval/fixtures.py`、`eval/recovery_suite.py`、`eval/runtime_suite.py`、
  `tests/eval/{conftest,test_runner_isolation,test_scorers}.py`：Listing 状态字面量
  与默认值改为 `PUBLISHED`；
- `eval/datasets/product_operations/*.yaml`：100 处 `status: ACTIVE` → `PUBLISHED`，
  并同步更新 `manifest.json` 的 `core_sha256` / `blind_sha256`（数据集内容变更，
  冻结摘要必须一致；正式的成绩冻结与数据集改造仍归 Task 13）。

迁移 DDL 的两个必要细节（计划原文未覆盖）：

- `listings.status` 在 0002 由非原生枚举生成 `VARCHAR(8)`，装不下
  `PUBLISHED`(9)/`UNPUBLISHED`(11)：0004 先扩列到 `VARCHAR(11)` 并把
  `server_default` 改为 `'PUBLISHED'`，再映射历史值、重建 CHECK；`downgrade()`
  反向操作。若只改 CHECK 不扩列，`UPDATE` 直接 `StringDataRightTruncation`；
- 历史映射用显式 `UPDATE ... WHERE status='ACTIVE'`，不静默丢弃（设计 §7）。

**验收条件**

- [x] 迁移 0004 建出 `fulfillment_locations` / `inventory_levels`；
  `ck_listings_status` 只允许 `PUBLISHED/UNPUBLISHED`（不含旧值）；
  `ck_inventory_levels_available_quantity` 为 `available_quantity >= 0`
  （`tests/persistence/test_migrations.py`）；
- [x] `ListingStatus` 只含 `PUBLISHED/UNPUBLISHED`；`FulfillmentLocation` /
  `InventoryLevel` 同 Workspace 唯一约束与非负库存 CHECK 生效
  （`tests/agents/product_operations/test_domain_schema.py`）；
- [x] `adjust_inventory`：正确 CAS 递增 version；过期版本 / 跨 Workspace /
  调整后为负三种情形均不命中返回 `None`；
  `set_listing_publication_state`：CAS 迁移发布状态、过期版本不命中；
- [x] 种子夹具提供 `fulfillment_location` 与 `inventory_level`
  （`available_quantity=20`、`version=1`）；
- [x] 全量门禁：`pytest` 889 passed、`ruff check .` 通过、`mypy runtime agents
  adapters composition apps eval` 通过（222 files）。

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
- Create: `agents/product_operations/input/references.py`（店铺引用解析，见下）
- Modify: `agents/product_operations/input/profiles.py`
- Modify: `agents/product_operations/shared/errors.py`、`shared/__init__.py`
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

**实现补充与偏差（落地时明确）**

1. **店铺引用解析的位置**：计划只说“Service 使用授权的 `StoreReferenceResolver`”，
   未定位置。实现放在 `agents/product_operations/input/references.py`——与既有
   “模型输入 → 可信内部值”解析（`input/requirements.py` 的别名 → ID）同层，
   避免把跨能力的引用解析塞进 `shared/`（`shared` 只放规范错误码、写回执不变量
   与 Reconciler 注册表）；
2. **新增规范错误码 `AMBIGUOUS_STORE_REFERENCE`**：店铺名称/别名可能撞车，与既有
   `AMBIGUOUS_LOCATION` 同类，故并入 `shared/errors.py` 与 `shared/__init__.py`；
3. **字段集合按版本区分**：新 `search_products`(v3) 与旧版(v2) 共用 stable key
   但字段不同（`store_refs` vs `store_ids`），因此 `P1_TOOL_ARGUMENT_FIELDS` /
   `p1_fields_for` 独立于旧的 `TOOL_ARGUMENT_FIELDS` / `fields_for`，旧映射不改，
   旧 Run 归一化行为不变；
4. **错误通道固定两类**：授权与资源范围拒绝沿用既有读路径的 `ToolAccessError`
   （PreToolUse 管线已识别），领域查找失败用 `ProductOperationError` 的规范码
   （`NOT_FOUND` / `AMBIGUOUS_STORE_REFERENCE`）。两类的边界在模块 docstring 写明；
5. **输出只给模型可见引用**：`ProductSearchItem.stores[].store_ref` 用店铺名、
   `ListingSnapshot.store_ref` 用店铺名，不返回内部 UUID；`search_products` 输出
   不含成本价。
6. **店铺级授权在 Service 内强制**：PreTool 的 `AuthorizationResourceResolver`
   只看 arguments，无法把名称/别名解析成 UUID，因此两个新 Tool 的
   `resource_mode="WORKSPACE"`，店铺范围由 `StoreReferenceResolver` 逐条校验并
   以 `RESOURCE_SCOPE_DENIED` 拒绝（不做静默收窄）。
7. **本任务不接线**：`SEARCH_PRODUCTS_DEFINITION`(v3) 与 `GET_LISTING_DEFINITION`
   (v1) 尚未进入 Agent/组合根装配；发布与装配属 Task 11/12。

**验收条件**

- [x] `SEARCH_PRODUCTS_DEFINITION` 输入 Schema 属性集合 == `{query, store_refs}`；
  `GET_LISTING_DEFINITION` == `{store_ref, sku}`——新 Tool 不暴露内部 ID；
- [x] `search_products` 只返回授权店铺内的 PUBLISHED Listing，按关键词过滤，
  引用支持名称与别名，输出不含成本价与内部 UUID，未发布 Listing 被排除；
- [x] `search_products`：越权引用 → `RESOURCE_SCOPE_DENIED`；未知引用 →
  `NOT_FOUND`；缺 `product.read` → `PERMISSION_DENIED`；无授权店铺 → 空结果；
- [x] `get_listing` 返回规范发布状态；未发布 Listing 仍可读；
  未知/歧义/越权引用与缺权限各自确定性失败；禁用商品以 `issues` 表达；
- [x] 全量门禁：`pytest` 908 passed、`ruff check .` 通过、`mypy runtime agents
  adapters composition apps eval` 通过（231 files）。

---

### Task 6 前置: Runtime 写入重试预算（Task 6 验收依赖）

**Files:**
- Modify: `runtime/orchestration/limits.py`
- Modify: `runtime/execution/reconciliation.py`
- Modify: `composition/product_operations.py`
- Modify: `agents/product_operations/agent.py`
- Modify: `tests/runtime/orchestration/test_limits.py`
- Modify: `tests/runtime/execution/test_reconciliation.py`
- Create: `tests/agents/product_operations/pricing/test_price_reconciliation.py`
  （与 Task 6 共用，见下方 Task 6 Step 4）

**背景（Task 2.3 评审结论）**

Task 2.3 已把"确定性执行失败"的重排收口为显式 `retryable` 契约，但
Reconciliation 的 `NOT_APPLIED → VALIDATING` 仍是**独立的、无上界**的重排入口：
`ReconciliationService.apply_result` 对 `ReconcileResult.NOT_APPLIED` 无条件把
ToolCall 回 `VALIDATING`，Run 回 `QUEUED`，模型可再次确认并再次执行同一
Operation。回查预算 `reconcile_budget` 只约束 `UNKNOWN` 的**回查次数**，不约束
写入**执行次数**，因此"外部写入反复 NOT_APPLIED"仍可无限重排。

本轮必须把该上界实现于**通用 Runtime**（不得写进 Pricing 业务模块），Task 6
只负责用价格恢复场景验收。

**锁定的规则**

- **预算来源**：`RunLimits` 新增 `write_retry_budget`（保守默认值，非法/非正回落
  默认，与既有 `_positive_int` 一致），经 `LoopBudget.write_retry_budget` 暴露，
  由组合根注入 `ReconciliationService`；
- **计数依据是持久化事实**：按 `OperationAttempt.kind == EXECUTE` 统计同一
  Operation 已发生的执行次数（`_count_execute_attempts`），不从内存计数推断，
  使恢复重入沿用同一预算；
- **`NOT_APPLIED` 由预算裁决**：`execute_attempt_count < write_retry_budget` 时
  才把 ToolCall 回 `VALIDATING`（仍必须重走完整 PreToolUse，`direct_execute`
  恒 False）；否则 ToolCall 落 `FAILED` 终态，Run 回 `QUEUED` 让模型看到失败并
  收尾——不得再创建可重排的 `VALIDATING`；
- **`UNKNOWN` 与 `APPLIED` 分支不变**：回查预算语义、UNKNOWN 保持权威事实、
  APPLIED → SUCCEEDED 均不受影响。

- [ ] **Step 1: 写 Runtime 预算失败测试**

`tests/runtime/orchestration/test_limits.py`：

```python
def test_write_retry_budget_is_exposed() -> None:
    budget = LoopBudget(RunLimits(write_retry_budget=1))
    assert budget.write_retry_budget == 1


def test_write_retry_budget_falls_back_on_invalid_override() -> None:
    limits = RunLimits.from_definition({"write_retry_budget": 0})
    assert limits.write_retry_budget >= 1
```

`tests/runtime/execution/test_reconciliation.py`：

```python
async def test_not_applied_returns_to_validating_only_within_budget(...) -> None:
    # 该 Operation 已有 0 个 EXECUTE Attempt(< 预算 1)→ ToolCall VALIDATING
    ...
    assert outcome.tool_call_status is ToolCallStatus.VALIDATING


async def test_not_applied_fails_tool_call_when_write_budget_exhausted(...) -> None:
    # 该 Operation 已有 1 个 EXECUTE Attempt(>= 预算 1)→ ToolCall FAILED
    ...
    assert outcome.tool_call_status is ToolCallStatus.FAILED
    assert outcome.next_job_kind is RunJobKind.ADVANCE_RUN
```

- [x] **Step 2: 运行测试并确认失败**

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/orchestration/test_limits.py tests/runtime/execution/test_reconciliation.py -q
```

Expected: FAIL，`RunLimits` 无 `write_retry_budget`，`NOT_APPLIED` 恒回 `VALIDATING`。

- [ ] **Step 3: 实现预算记账与 `NOT_APPLIED` 裁决**

`runtime/orchestration/limits.py`：

```python
@dataclass(frozen=True)
class RunLimits:
    ...
    write_retry_budget: int = 1

    @classmethod
    def from_definition(cls, loop_limits):
        ...
        write_retry_budget=_positive_int(overrides, "write_retry_budget", 1),
```

```python
@dataclass
class LoopBudget:
    @property
    def write_retry_budget(self) -> int:
        return self.limits.write_retry_budget
```

`runtime/execution/reconciliation.py`：构造参数新增 `write_retry_budget: int`，
新增 `_count_execute_attempts(operation_row_id)`（`kind == EXECUTE` 计数），在
`ReconcileResult.NOT_APPLIED` 分支中：

```python
elif result is ReconcileResult.NOT_APPLIED:
    operation_status = OperationStatus.NOT_APPLIED
    executed = await self._count_execute_attempts(operation_row.id)
    tool_call_status = (
        ToolCallStatus.VALIDATING
        if executed < self._write_retry_budget
        else ToolCallStatus.FAILED
    )
```

ToolCall `FAILED` 时 Run 仍走 `WAITING_RECONCILIATION → QUEUED`（返回
`ADVANCE_RUN`），模型据失败结果收尾；`FAILED` 不写入 `ToolCall` 重排入口。

`composition/product_operations.py` 注入 `write_retry_budget=self._run_limits.write_retry_budget`；
`agents/product_operations/agent.py` 的 `P1_LOOP_LIMITS` 增
`"write_retry_budget": 1`（Task 11 定稿时同步）。

- [x] **Step 4: 定点回归**

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/orchestration/test_limits.py tests/runtime/execution/test_reconciliation.py tests/runtime/execution/test_operations.py -q
```

Expected: PASS。

**验收条件**

- [ ] `RunLimits.write_retry_budget` 存在、非法覆盖回落默认、经 `LoopBudget` 暴露；
- [ ] `ReconciliationService` 按 `OperationAttempt(kind=EXECUTE)` 持久化次数裁决
  `NOT_APPLIED` 分支：预算未耗尽 → ToolCall `VALIDATING`；预算耗尽 → ToolCall
  `FAILED`（终态），Run 仍回 `QUEUED` 且 `next_job_kind=ADVANCE_RUN`；
- [ ] 预算耗尽后**不再产生可重排的 `VALIDATING`**，同一 Operation 不会无限重排；
- [ ] 预算实现位于 `runtime/`，Pricing 模块**不含**任何重试计数逻辑；
- [ ] `tests/agents/product_operations/pricing/test_price_reconciliation.py`
  （Task 6 Step 4 建立）用价格恢复场景证明：连续 `UNKNOWN → NOT_APPLIED` 到预算
  耗尽后 ToolCall 落 `FAILED`、Adapter 写入次数不超过预算、`direct_execute` 恒
  False；
- [x] 全量门禁：`pytest` 全绿、`ruff check .` 通过、`mypy runtime agents adapters
  composition apps eval` 通过。

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

- [x] **Step 1: 写价格比较和绝对设置失败测试**

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

- [x] **Step 2: 定义 Pricing Gateway 和命令**

```python
class PricingGateway(Protocol):
    async def get_price(self, query: PriceQuery) -> PriceSnapshot: ...
    async def set_price(self, command: SetListingPriceCommand) -> WriteReceipt: ...
    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult: ...
```

`SetListingPriceCommand` 必须包含 `operation_id`、可信内部资源、`target_price`、`currency`、`expected_version`；其 `idempotency_key` 由 Runtime 参数注入，不从 Tool input 读取。

- [x] **Step 3: 实现确定性价格比较**

同币种时计算最低价、最高价和价差；存在多个币种时返回 `CURRENCY_MISMATCH`，不得由模型自行换算。

- [x] **Step 4: 实现 `set_listing_price` prepare/execute**

`prepare()`：

```text
解析 store_ref → 读取 Listing → 执行 PricingPolicy.validate_execution
→ 构造 SetListingPriceCommand
→ 返回 expected_state(operation_type=SET_LISTING_PRICE)
```

`execute_prepared()` 只调用 `PricingGateway.set_price(command)` 并转换 `WriteReceipt`。

- [x] **Step 5: 实现价格 Reconciler**

只使用 `Operation.expected_state` 构造 `ReconcilePriceCommand`；不得从最新对话、当前 Agent Prompt 或模型文本恢复目标价格。

- [x] **Step 6: 迁移 Mock/REST Adapter 的价格接口**

`MockCommerceAdapter` 和 `RestCommerceAdapter` 实现 `PricingGateway`。REST 路径、安全主机、响应大小限制保持不变；旧 `execute/reconcile` 暂由兼容包装调用新方法。

补充：`adapters → agents.product_operations.pricing.gateway` 这条新依赖暴露了
`input` ↔ `tools` 的模块级循环导入（`input.requirements`/`input.references`
→ `tools.__init__` → `tools.compare_listing_prices` → `input.profiles` →
`input.requirements`）。在 `input/references.py` 与 `input/requirements.py`
把 `tools.access` / `tools.errors` 改为**调用点导入**打破环，不改变任何对外行为。

- [x] **Step 7: 运行价格全链路测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/pricing tests/adapters/commerce/test_mock_adapter.py tests/adapters/commerce/test_rest_adapter.py -q
git add agents/product_operations/pricing adapters/commerce/mock_adapter.py adapters/commerce/rest_adapter.py tests/agents/product_operations/pricing tests/adapters/commerce
git commit -m "refactor: establish pricing capability module"
```

Expected: PASS，提交成功。

---

### Task 6.1: 收口回查/Pricing/多租户约束缺陷（Task 7 复用前置）

Task 6 门禁全绿（943 passed），但复评发现 1 个 P0、3 个 P1。Inventory（Task 7）
会复用同一套回查、拒绝码、计数与外键模式，因此先做一个不回滚既有提交的独立收口。

**缺陷与证据（复评原文）：**

- [P0] **回查终态没有生成 ToolResult**：Reconciliation 把 ToolCall 改成
  SUCCEEDED/FAILED 并重排 Run，却没有持久化与原始 assistant tool call 配对的
  tool message。下一轮真实模型看到的是一组「有 tool call 无 tool result」的历史，
  OpenAI/Anthropic 可能直接拒绝，模型也无法在 APPLIED 或预算耗尽后正常收尾。
- [P1] **所有 Pricing 拒绝都被伪装成 LISTING_NOT_FOUND**：`prepare` 在拿到
  `None` 时一律返回 `LISTING_NOT_FOUND`，但 `None` 可能来自参数非法、无匹配门店、
  别名歧义、越权门店、低于价格下限、价格被锁、价格未变更。应返回带规范
  `error_code`/`context` 的类型化拒绝，而不是统一伪装成资源不存在。
- [P1] **`reconcile_count` 使用了全局 `attempt_no`**：写入事件用的是
  `attempt_no`，它跨越 EXECUTE 与 RECONCILE 两类 Attempt。一次真实执行加首次回查
  会被记成 `reconcile_count=2`，污染审计与恢复指标。
- [P1] **新表没有数据库级 Workspace 归属约束**：`workspace_id` 与 `store_id` 是两个
  独立外键，Workspace A 的 location 可以引用 Workspace B 的 store；
  inventory_levels 的 location/product 同理。应用层过滤无法替代多租户的库级不变量。

**Files:**
- Modify: `runtime/execution/reconciliation.py`
- Modify: `runtime/execution/operations.py`
- Modify: `agents/product_operations/pricing/service.py`
- Modify: `agents/product_operations/persistence/models.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Create: `migrations/versions/0005_product_operations_workspace_fks.py`
- Modify: `tests/runtime/execution/test_reconciliation.py`
- Modify: `tests/agents/product_operations/pricing/test_set_listing_price.py`
- Modify: `tests/agents/product_operations/test_domain_schema.py`
- Modify: `tests/persistence/test_migrations.py`

- [x] **Step 1: APPLIED / 终态 NOT_APPLIED 原子持久化一次 ToolResult**

`reconciliation.py` 在 ToolCall 进入终态（SUCCEEDED/FAILED）时调用
`_persist_terminal_tool_result`，以 `call_id` 为幂等键：已存在同 `call_id` 的 TOOL
消息则跳过，重复恢复不重复写消息。在途（VALIDATING）与 UNKNOWN 分支不写。

- [x] **Step 2: 预算耗尽使用稳定错误码 `WRITE_RETRY_BUDGET_EXCEEDED`**

NOT_APPLIED 且 `executed >= write_retry_budget` 时，`failure_code` 固定为
`WRITE_RETRY_BUDGET_EXCEEDED`，同时写入 `ToolCall.failure_code` 并作为 tool 结果
返回模型。

- [x] **Step 3: Reconciliation 更新一致的最新回执（stage=RECONCILIATION）**

`_update_operation_receipt` 写入 `stage=RECEIPT_STAGE_RECONCILIATION` 的
`last_receipt`；原始 EXECUTION 证据保留在 EXECUTE Attempt 与 append-only
Events/审计中，不依赖可覆盖的 `last_receipt`。

- [x] **Step 4: 修正 `reconcile_count`**

返回值、AgentEvent、AuditEvent 统一使用按 kind=RECONCILE 计数的
`reconcile_count`；`attempt_no` 仅作为 OperationAttempt 行序号。

- [x] **Step 5: Pricing `prepare` 保留真实拒绝原因**

`_resolve`/`_resolve_store` 返回类型化 `_Rejection`：参数非法→
`VALIDATION_FAILED(INVALID_ARGUMENTS)`；缺失→`LISTING_NOT_FOUND`；越权→
`RESOURCE_SCOPE_DENIED`；策略拒绝映射 `VALIDATION_FAILED + rule_code`
（`PRICE_BELOW_FLOOR`/`PRICE_UNCHANGED`）或 `VERSION_CONFLICT`；别名歧义→
`AMBIGUOUS_STORE_REFERENCE`。

- [x] **Step 6: 组合 Workspace 外键迁移 + 跨 Workspace 插入失败测试**

迁移 `0005` 先 `_assert_no_cross_workspace_references()` 失败即停，再为
stores/products/listings/fulfillment_locations 加 `(workspace_id, id)` 唯一键，
最后把 6 条子表外键改为组合 `(workspace_id, child_col) → (workspace_id, id)`。

- [x] **Step 7: 修正 `MockCommerceAdapter` 过时注释**

移除「持续失败可确定重试」表述，澄清 FAILED/UNKNOWN 落入终态 ToolCall 而非自动重试。

- [x] **Step 8: 运行全链路并提交**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy .
git add runtime/execution/reconciliation.py runtime/execution/operations.py agents/product_operations/pricing/service.py agents/product_operations/persistence/models.py adapters/commerce/mock_adapter.py migrations/versions/0005_product_operations_workspace_fks.py tests/runtime/execution/test_reconciliation.py tests/agents/product_operations/pricing/test_set_listing_price.py tests/agents/product_operations/test_domain_schema.py tests/persistence/test_migrations.py
git commit -m "fix: close reconciliation, pricing rejection, and tenancy gaps"
```

Expected: 960 passed（943 + 17 新增），ruff/mypy 全绿，提交成功。

**验收测试：**
- `UNKNOWN→APPLIED→ADVANCE_RUN` 后存在配对的 tool message。
- `UNKNOWN→NOT_APPLIED` 预算耗尽后 tool message 携带 `WRITE_RETRY_BUDGET_EXCEEDED`。
- 重放不产生重复 ToolResult。
- 首次真实回查 `reconcile_count==1`（即使已有 EXECUTE Attempt）。
- 越权门店 / 价格规则拒绝不再返回 `LISTING_NOT_FOUND`。
- 数据库拒绝跨 Workspace 的 location/inventory/listing 插入。

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

- [x] **Step 1: 写位置解析和幂等失败测试**

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

- [x] **Step 2: 定义 Tool 和 Gateway**

```python
STABLE_KEY_GET_INVENTORY_LEVEL = "get_inventory_level"
STABLE_KEY_ADJUST_INVENTORY_QUANTITY = "adjust_inventory_quantity"

class InventoryGateway(Protocol):
    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot: ...
    async def adjust_inventory(self, command: AdjustInventoryCommand) -> WriteReceipt: ...
    async def reconcile_inventory(self, command: ReconcileInventoryCommand) -> ReconcileResult: ...
```

- [x] **Step 3: 实现 InventoryPolicy**

规则必须覆盖：`delta != 0`、调整后数量非负、位置属于店铺、原因属于受控枚举、资源版本未变化。原因枚举固定为：

```python
class InventoryAdjustmentReason(str, enum.Enum):
    MANUAL_CORRECTION = "MANUAL_CORRECTION"
    RECEIPT = "RECEIPT"
    DAMAGE = "DAMAGE"
    RETURN = "RETURN"
```

- [x] **Step 4: 实现 prepare/execute 和 expected_state**

库存 `expected_state` 必须包含 `quantity_before`、`delta`、`expected_quantity_after`、`expected_version`、位置和原因。Adapter 回执丢失后禁止直接重放 `delta`。

- [x] **Step 5: 实现 Mock Adapter 与 Reconciler**

Mock Adapter 按 operation_id 去重，用 CAS 更新数量。Reconciler 同时验证操作记录和预期调整后数量；无法区分并发变化时返回 `UNKNOWN`，不得猜测 `APPLIED`。

- [x] **Step 6: 运行库存测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/inventory -q
git add agents/product_operations/inventory adapters/commerce/mock_adapter.py tests/agents/product_operations/inventory
git commit -m "feat: add inventory adjustment capability"
```

Expected: PASS，提交成功。

**实现说明(与计划原文的偏差):**
- `operation_type` 取 `ADJUST_INVENTORY`;新增权限 `product.inventory.write`(与
  `product.price.write` 对称)。`P1_TOOL_ARGUMENT_FIELDS` 已预置两个 Inventory
  key 的字段集,未改动。
- 位置引用解析 `resolve_location_for_store` 放在 `inventory/service.py`(与
  `pricing/policy.py` 的 `resolve_listing_for_sku` 同构):候选为店铺内 ACTIVE
  位置,未给出且唯一则自动解析,多位置 → `AmbiguousLocationError`(规范码
  `AMBIGUOUS_LOCATION`)。
- 类型化拒绝沿用 Task 6.1 的 `_Rejection`:缺 Listing/位置 → `NOT_FOUND`,歧义
  店铺/位置 → `AMBIGUOUS_STORE_REFERENCE`/`AMBIGUOUS_LOCATION`,越权 →
  `RESOURCE_SCOPE_DENIED`,策略拒绝 → `VERSION_CONFLICT` 或
  `VALIDATION_FAILED + rule_code`(`ZERO_DELTA`/`NEGATIVE_RESULT`/... )。
- 计划外的小改动:`ProductRepository` 新增 `get_fulfillment_location` 与
  `get_inventory_level` 两个只读 getter(Mock Adapter 读库存/校验所必需);
  库存 side 的 reason 码 `INVENTORY_NOT_FOUND` 定义在 `inventory/gateway.py`,
  不改 `adapters/commerce/protocol.py`。
- 测试新增 `tests/agents/product_operations/inventory/_support.py`(共享种子/
  Actor/测试 Gateway)与 `conftest.py`(`inventory_env` 夹具);放在 `_support`
  而非 conftest 是为了让 mypy 与 pytest 都能按目录解析导入,避免多层
  `conftest` 同名冲突。
- **Adapter 就地刷新坑**:`ProductRepository` 的 CAS 走 `populate_existing` /
  `RETURNING`,会把身份映射中的 `InventoryLevel` 就地刷成写后值,测试必须在
  调用前捕获 `quantity_before`/`version` 标量,不能事后读 `env.level`。
- 按计划 Task 11/12 才把能力接入 Product Agent 与组合根,本任务不接线。


---

### Task 7.1: 收口 P1 写目标来源守卫与库存回查绑定（Task 8 复用前置）

Task 7 门禁全绿（993 passed），但复评发现 2 个 P1，且指向同一类问题：**写目标的
"来源"从未进入守卫，只证明了"存在且有权"**。Task 8 的上下架操作同样使用
`store_ref`，继续复制会扩大问题，因此先做一个不回滚既有提交的独立收口。

**缺陷与证据（复评原文）：**

- [P1] **新写 Tool 的业务目标没有进入 Provenance 守卫**：`profiles.py` 仍只守卫
  `store_id`、`sku`，但 P1 写 Tool 已改用 `store_ref`，库存还增加了 `location_ref`。
  模型自行补一个已授权的 `store_ref` 可能通过；多仓场景下模型自行补一个合法
  `location_ref` 会绕过"多位置必须补充输入"。Handler 的授权与解析只能证明"这个
  店/仓存在且有权操作"，不能证明"这是用户选择的写入目标"。
- [P1] **库存回查没有验证 Operation 与原资源完全绑定**：`_OperationRecord` 已保存
  `workspace_id`/`location_id`，但回查只比较 `applied_quantity`，也没有记录
  `product_id`。同一个 `operation_id` 被错误地拿去回查另一个库存资源时，只要目标
  数量碰巧相同就可能被判成 `APPLIED`。恢复契约应能抵御装配错误、错路由与脏数据，
  不能依赖"上游肯定不会传错"。

**Files:**
- Modify: `agents/product_operations/input/profiles.py`
- Modify: `agents/product_operations/input/provenance.py`
- Modify: `agents/product_operations/input/requirements.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Modify: `adapters/commerce/protocol.py`（新增 `IDEMPOTENCY_KEY_REUSED` 原因码）
- Modify: `tests/agents/product_operations/test_argument_provenance.py`
- Modify: `tests/adapters/commerce/test_mock_adapter.py`
- Modify: `tests/agents/product_operations/inventory/test_inventory_recovery.py`
- Create: `tests/agents/product_operations/inventory/test_inventory_write_guard.py`

- [x] **Step 1: 证据引擎识别 `store_ref` / `location_ref`**

`provenance.py` 新增 `LocationCandidate`（`location_id`/`store_id`/`name`/`aliases`），
`EvidenceResolver.resolve` 增加 `locations` 入参，并复用/新增两个判定：

```python
field_sources["store_ref"] = self._resolve_store(field="store_ref", ...)  # 只取来源等级
field_sources["location_ref"] = self._resolve_location(...)               # 缺省不参与判定
```

`store_ref` 与 `store_id` 同规则（USER_EXPLICIT / USER_CONFIRMED，其余 UNTRUSTED）；
**`store_ref` 不做规范化改写**——`StoreReferenceResolver` 只接受名称/别名，不接受
内部 UUID，因此证据引擎只回传来源等级，不回填 `str(store.id)`。

- [x] **Step 2: 只有"模型实际提供"的写目标才要求来源证明**

`requirements.py` 的 `untrusted` 计算拆成两段：`key_fields`（`store_id`/`sku`/
`target_price`，缺失即要求补充，沿用旧契约）与 `provided_provenance`
（`store_ref`/`location_ref`，**仅在 `field in normalized` 时**参与）。

```python
provided_provenance = tuple(
    field for field in fields
    if field in _P1_PROVENANCE_FIELDS and field in normalized
)
```

这样"唯一启用位置由 Service 确定性解析"与"多位置由 Service 要求补充"不被守卫破坏；
`_build_requirement` 额外给出 `store_ref` 候选项（值为店铺**名称**，可被
`store_ref` 解析，而非内部 UUID）。

- [x] **Step 3: 守卫字段集合纳入 P1 写目标**

```python
PROVENANCE_GUARD_FIELDS: Final = ("store_id", "store_ref", "sku", "location_ref")
```

`ProductPreToolProfileFactory` 已按 `field in self._fields` 过滤，故各 Tool 只守卫
自身字段集合内的写目标；管线守卫 `_apply_provenance_guard` 本就只对"已提供"
（`field in arguments`）生效。

- [x] **Step 4: 写 / 回查两条路径都绑定完整资源身份**

`_OperationRecord` 增加 `resource_identity`（规范化资源元组）与 `product_id`：

- **回查路径**：`_reconcile_price` 匹配 `workspace_id + listing_id`，
  `_reconcile_inventory` 匹配 `workspace_id + location_id + product_id +
  applied_quantity`；错 Workspace、错资源、数量/价格恰好相同一律 `NOT_APPLIED`
  （不猜 `APPLIED`）；
- **写路径**：同一 `operation_id` 被用于**另一个资源**时确定失败
  （`IDEMPOTENCY_KEY_REUSED`，新增于 `adapters/commerce/protocol.py`，
  非可重试），不回放旧回执；资源身份一致才按平台幂等契约回放。

判定只看**资源身份**，不看 `expected_version` / `target_price` / `delta` 等会随
首次写入自然变化的载荷字段——否则同一次重复提交（重读后 `expected_version` 已 +1）
会被误判成冲突，破坏 safety matrix 的 `duplicate_submit` 契约。首版曾用"整条命令
指纹"判定，正是因此触发 `duplicate_submit` 红灯，已收窄为资源身份。

- [x] **Step 5: 回归测试与门禁**

- 域证据：`test_argument_provenance.py` 6 例（store_ref 无证据→Input、消息点名→
  USER_EXPLICIT、回答→USER_CONFIRMED、location_ref 无证据→Input、点名→
  USER_EXPLICIT、缺省不要求）；
- 管线守卫：`test_inventory_write_guard.py` 4 例（守卫字段集合；无证据 store_ref →
  `REQUIRE_INPUT` 且 Adapter 调用为 0；无证据 location_ref 同理；可信目标放行至
  `REQUIRE_CONFIRMATION`）；
- Adapter 回查反例：`test_inventory_recovery.py` 4 例（错位置 / 错商品 / 错 Workspace
  且数量相同 → `NOT_APPLIED`，同目标对照 → `APPLIED`）与 `test_mock_adapter.py`
  1 例（错 Listing 且价格相同 → `NOT_APPLIED`）；
- Adapter 写路径冲突：`test_inventory_recovery.py`
  `test_reused_operation_id_on_another_location_fails_closed` 与
  `test_mock_adapter.py` `test_reused_operation_id_on_another_listing_fails_closed`
  2 例（同 `operation_id` 换资源 → `FAILED` + `IDEMPOTENCY_KEY_REUSED`，逻辑写不增）。

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations tests/adapters/commerce -q
```

Expected: PASS。全量 1010 passed（993 + 17 新增）/ ruff 通过 / mypy 源包 245 files。


---

### Task 7.2: 拆分幂等资源身份与业务意图，位置证据按店铺作用域匹配，并接通多仓业务输入需求

Task 7.1 门禁全绿，但复评连续两轮指出未收口点，方向正确、不要求回滚既有提交。

第一轮的两处都不是"写错了"，而是**判定粒度太粗**：一个把"同一个资源"当成了
"同一个业务动作"，另一个把"整个 Workspace 的位置集合"当成了"目标店铺的位置集合"。

第二轮（本轮）指出：Task 7.2 首版新加的"多位置在 PreToolUse 直接要求补充"**没有
真正接入 Runtime**，并且位置候选会越过授权边界：

- [P1] `resolve()` 生成的 `InputRequirement` 被 `ProductPreToolNormalizer` 与
  `ProductPreToolProvenanceResolver` 丢弃；`location_ref` 在 Tool Schema 中可选，
  Schema 阶段不产生诊断，因此真实管线永远不会返回 `REQUIRE_INPUT`。
- [P1] 即便修通链路，`AgentLoop` 创建持久化 Input Request 时没有传递
  `requirement.options`，Workbench 看不到位置/店铺候选项。
- [P1] `store_scope` 来自 Workspace 全量店铺证据，可能指向 Actor 越权店铺，而
  `_location_options()` 在 Access Control **之前**返回该店铺的位置名称。

**缺陷与证据（第一轮复评原文）：**

- [P1] **幂等冲突只比较资源，没有比较业务意图**：`mock_adapter.py` 的写路径身份是
  改价 `workspace_id + listing_id`、库存 `workspace_id + location_id + product_id`。
  于是同一个 `operation_id`：同一 Listing `99 → 199` 仍回放第一次成功；同一库存
  `delta=5 → delta=10` 仍回放第一次成功；`currency=CNY → USD` 甚至会在币种校验前
  回放成功；调整原因变化也不产生冲突。复评用当前代码直接验证：
  `inventory identity equal for delta 5 vs 10: True`、
  `price identity equal for target 99 vs 199: True`。
- [P1] **位置 Provenance 没有按已选店铺缩小候选范围**：`provenance.py` 已解析出
  `store_ref` 对应的规范店铺 ID，却用 `_` 丢弃；随后 `location_ref` 在整个
  Workspace 的位置集合中匹配。真实多店场景（淘宝旗舰店 → 默认仓；抖音小店 →
  默认仓）下，用户明确说"淘宝旗舰店默认仓增加 5 件"，仍得到
  `store_ref = USER_EXPLICIT` / `location_ref = UNTRUSTED`。

**Files:**
- Modify: `adapters/commerce/mock_adapter.py`
- Modify: `adapters/commerce/protocol.py`（`IDEMPOTENCY_KEY_REUSED` 文档语义收口）
- Modify: `agents/product_operations/input/provenance.py`
- Modify: `agents/product_operations/input/requirements.py`
- Modify: `agents/product_operations/input/profiles.py`（把用户消息/回答传给输入需求适配器）
- Modify: `runtime/execution/pretool/pipeline.py`（业务输入检查契约）
- Modify: `runtime/execution/pretool/decisions.py`（`require_input` 允许无诊断）
- Modify: `runtime/execution/pretool/profiles.py`（空实现对齐新签名）
- Modify: `runtime/orchestration/agent_loop.py`（持久化候选项）
- Modify: `apps/api/routes/sessions.py`（候选项注释与兜底语义收口）
- Modify: `tests/adapters/commerce/test_mock_adapter.py`
- Modify: `tests/agents/product_operations/inventory/test_inventory_recovery.py`
- Modify: `tests/agents/product_operations/inventory/test_inventory_write_guard.py`
- Modify: `tests/agents/product_operations/test_argument_provenance.py`
- Add: `tests/agents/product_operations/test_location_requirement_scope.py`（无数据库用例）
- Modify: `tests/runtime/execution/test_pretool_pipeline.py`
- Modify: `tests/runtime/execution/test_argument_trust_levels.py`
- Modify: `tests/runtime/orchestration/test_agent_loop.py`

- [x] **Step 1: 把"同一个资源"与"同一个业务动作"拆成两个正交概念**

`_OperationRecord` 同时保留两个指纹：

```python
resource_identity  # 回查路径使用:只证明是不是同一个资源
operation_intent   # 写路径使用:证明是不是同一个业务动作
```

- **`resource_identity`（回查）**：价格 `workspace_id + listing_id`，库存
  `workspace_id + location_id + product_id`。`_reconcile_price` /
  `_reconcile_inventory` 改为直接比对该元组（库存侧再叠加
  `applied_quantity == expected_quantity_after`），错资源一律 `NOT_APPLIED`。
- **`operation_intent`（写路径）**：在资源身份之上并入外部引用与目标值，
  价格再加 `target_price` / `currency`，库存再加 `delta` /
  `expected_quantity_after` / `reason`。写路径逐项比对：意图不同 → 确定失败
  `IDEMPOTENCY_KEY_REUSED`，零逻辑写，绝不回放旧成功。

两个指纹都**排除** `expected_version`：它属于"基于哪一次读取"的乐观锁版本，
重复提交会重读资源而自然 +1，纳入即会把正常重复提交误判成冲突（safety matrix 的
`duplicate_submit` 契约）。这与 Task 7.1 首版"整条命令指纹"回退的教训一致——区别
在于**排除的只有乐观锁版本，目标值/增量/币种/原因必须留下**。

- [x] **Step 2: 位置证据按已解析店铺收窄候选**

`EvidenceResolver.resolve` 不再丢弃 `store_ref` 的规范店铺，而是与 `store_id` 一起
得出 `store_scope`：

```python
store_scope = store_value if store_value is not None else store_ref_value
```

`_resolve_location` 先按 `store_scope` 过滤 `LocationCandidate`，再在**目标店铺内**
做名称/别名唯一性比较。`ProvenanceOutcome` 新增 `store_scope`，供调用方复用。
作用域缺失（店铺本身不可信）时退回全 Workspace 匹配，跨店铺同名仓仍按歧义处理。

- [x] **Step 3: 多位置时在 PreToolUse 直接要求补充**

`requirements.py` 新增 `_requires_location_choice`：工具声明了 `location_ref`、
模型未提供该字段、目标店铺已确定且该店铺存在**多个**启用位置时，直接并入
`untrusted`，在 PreToolUse 阶段产生要求补充 `location_ref` 的 Input Request，
不再等进入 Handler 后才确定性失败。`requirement_for` 同时给出目标店铺内的
位置候选项（值为位置名称，可被 `location_ref` 解析）。

- [x] **Step 4: 扩展 PreToolUse 业务输入检查契约（让 Step 3 真正接入 Runtime）**

首版问题不在规则本身，而在**契约**：`resolve()` 产出的 `requirement` 被
`ProductPreToolNormalizer` 与 `ProductPreToolProvenanceResolver` 丢弃，而
`ProductPreToolInputRequirementResolver` 只接收 `SchemaDiagnostic`；`location_ref`
在 Schema 中可选，Schema 阶段不产生诊断，于是业务需求永远到不了管线。选择完整
方案而不是回退 P2：

- `InputRequirementResolver` 协议放宽为
  `resolve(*, diagnostic: SchemaDiagnostic | None, arguments)`；`input_requirement`
  阶段**即使没有诊断也咨询业务层**（`input_normalization` 的规范化参数一并传入）。
  这样"Schema 可选、却是执行前提"的字段（多位置店铺缺 `location_ref`）才能变成
  `REQUIRE_INPUT`。
- `require_input` 的 `schema_diagnostic` 改为可选：无诊断时 `detail.field_paths`
  取 `InputRequirement.field_paths`；有诊断时保持原语义（仍取诊断字段）。
- 适配层两条来源：**诊断驱动**沿用原逻辑；**无诊断**时复用域层同一份判定
  （`ProductInputRequirementResolver.resolve`），不重复实现规则。只有声明了
  `location_ref` 的工具会在无诊断时触发域层判定，其余 Tool 提前短路，不增加
  权威读取。

- [x] **Step 5: 持久化候选项，并让位置候选不越过授权边界**

- `AgentLoop._handle_require_input` 把 `requirement.options` 传给
  `InputService.create(options=...)`（服务已支持，此前**从未传入**，因此店铺/
  位置候选在 API 与 Workbench 一律为空）；无候选时保持 `NULL`，保留"服务端未给
  候选"的语义。`apps/api/routes/sessions.py` 的注释与兜底同步收口。
- 位置候选与"多位置要求补充"都以**授权店铺**为界：新增
  `_store_in_actor_scope`，`_requires_location_choice` 与 `requirement_for` 的
  `location_ref` 分支都先校验 `store_scope` 在 Actor 范围内。越权店铺不生成
  位置候选、也不向用户追问，交由 Access Control 以资源范围确定性拒绝。

- [x] **Step 6: 回归测试与门禁**

- 无数据库用例（`test_location_requirement_scope.py`，新增）：`_requires_location_choice`
  的授权闸门与其它条件（已提供 `location_ref` / 单位置 / 没有该字段 / 店铺不可信）；
  `requirement_for` 不为越权店铺产出位置候选；适配层在无诊断时接通业务需求、
  不涉位置时短路；`require_input` 无诊断时的 `detail.field_paths` 语义。这组用例
  在 PostgreSQL 未启动时同样可执行；
- 管线契约（`test_pretool_pipeline.py`）：无 `SchemaDiagnostic` 的业务需求 →
  `REQUIRE_INPUT`，且 `detail.field_paths` 取自需求本身；`test_argument_trust_levels.py`
  的替身对齐新签名；
- 真实管线（`test_inventory_write_guard.py`）：店铺多个启用位置 + 缺省
  `location_ref` → 真实管线 `REQUIRE_INPUT`（无诊断）且 Adapter 调用为 0；
  越权店铺 → 不生成位置候选、`RESOURCE_SCOPE_DENIED`；
- 域证据（`test_argument_provenance.py`）：两店同名"默认仓" + 店铺已明确 →
  `location_ref` 为 `USER_EXPLICIT`；店铺证据缺失时仍按歧义处理（对照组）；
  目标店铺多位置且缺省 `location_ref` → `field_paths == ("location_ref",)`
  且候选项限定在目标店铺内；越权店铺 → 不产生位置需求、候选项不含 `location_ref`；
- 候选项持久化（`test_agent_loop.py`）：`REQUIRE_INPUT` 的 `options_json` 落库；
  无候选时落 `NULL`；
- Adapter 写路径意图冲突：`test_mock_adapter.py` 改目标价 / 换币种 2 例 +
  `expected_version` 重读仍回放的对照面；`test_inventory_recovery.py` 改增量 /
  改原因（参数化）→ `FAILED` + `IDEMPOTENCY_KEY_REUSED`，逻辑写不增，并补
  `expected_version` 重读仍回放的对照面。

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations tests/adapters/commerce -q
```

Expected: PASS。`ruff check .` 通过 / `mypy runtime agents adapters apps eval` 241 files 通过。

实际结果（2026-10-14，PostgreSQL 由 `docker compose up -d postgres` 提供）：

- 聚焦 `tests/agents/product_operations tests/adapters/commerce`：**286 passed**；
- 全量 `pytest -q`：**1036 passed**；
- 无数据库子集（新增纯逻辑用例 + 既有管线单元测试）：34 passed，可在 PostgreSQL
  未启动时独立运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/runtime/execution tests/agents/product_operations/test_location_requirement_scope.py -q
```


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
- Create: `tests/agents/product_operations/listing/publication_support.py`
- Create: `tests/agents/product_operations/listing/conftest.py`

- [x] **Step 1: 写发布状态幂等失败测试**

```python
async def test_publish_already_published_listing_is_successful_noop(env) -> None:
    handler = env.make_publish_handler()
    outcome = await handler.prepare(...)
    result = await handler.execute_prepared(outcome.prepared)
    assert result.status is WriteStatus.APPLIED
    assert result.changed is False
```

同目标（已发布再发布 / 已取消再取消）是**成功 no-op**，不是错误：`APPLIED`
+ `changed=False` + **0 逻辑写**。`changed` 是新增在
`ListingPublicationResult` 上的模型可见信号，让上层能区分"确实改了"和"本来就是
这个状态"。

- [x] **Step 2: 定义 ListingGateway**

```python
class ListingGateway(Protocol):
    async def get_listing(self, query: ListingQuery) -> ListingSnapshot: ...
    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt: ...
    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt: ...
    async def reconcile_listing(self, command: ReconcileListingCommand) -> ReconcileResult: ...
```

方向**不进命令**：`publish_listing`/`unpublish_listing` 是两个独立 Port 方法，
`ChangePublicationCommand` 不含 `target_state` 字段——从结构上杜绝"方法方向与命令
方向互相矛盾"。

- [x] **Step 3: 实现状态规则**

```python
_ALLOWED_TARGETS: Final[dict[ListingStatus, frozenset[ListingStatus]]] = {
    ListingStatus.PUBLISHED: frozenset({ListingStatus.PUBLISHED, ListingStatus.UNPUBLISHED}),
    ListingStatus.UNPUBLISHED: frozenset({ListingStatus.PUBLISHED, ListingStatus.UNPUBLISHED}),
}
```

`ListingPolicy` 规则顺序：能力（缺失 → `CAPABILITY_UNSUPPORTED`）→ reason 合法性
（`INVALID_REASON`）→ Listing 存在（`LISTING_NOT_FOUND`）→ 状态迁移
（`INVALID_STATE_TRANSITION`）。取消发布只翻转 `listings.status`，**不删除商品、
不清零库存**（库存是另一个事实）。`UnpublishReason` 仅 `MANUAL`/`COMPLIANCE`/
`SEASONAL_END`，刻意不含"缺货"——缺货是库存事实，不是取消发布理由。

- [x] **Step 4: 实现 prepare/execute 和 Reconciler**

`PreparedChangePublication` 保存 listing_id、目标发布状态（`TARGET_STATE`）、
`expected_version` 和 reason；`ChangePublicationHandler` 基类在 `prepare()` 返回
类型化 `_Rejection`（error_code + rule_code + detail），`execute_prepared()` 只依据
已 prepared 的命令调 `_call_gateway()`，按 `TARGET_STATE` 分发到对应 Port 方法。
`PublishListingHandler` / `UnpublishListingHandler` 是 `TARGET_STATE` 不同的两个
子类，共享 `OPERATION_TYPE = "CHANGE_LISTING_PUBLICATION"`
（`STABLE_KEY_PUBLISH_LISTING` / `STABLE_KEY_UNPUBLISH_LISTING` 区分工具）。

`build_reconcile_listing_command()` 对缺失字段、错误 `operation_type`、非枚举
`target_state` 一律 **fail-closed**；`ListingReconciler` 只把平台刊登状态翻译成
Runtime 三态结论。

- [x] **Step 5: 运行 Listing 测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations/listing -q
git add agents/product_operations/listing adapters/commerce/mock_adapter.py tests/agents/product_operations/listing
git commit -m "feat: add listing publication capability"
```

Expected: PASS，提交成功。

---

**实际实现补充（2026-10-14）：**

- **幂等三件套与 Task 7.2 对齐**：价格写入的 `_price_resource_identity` 提升为共享的
  `_listing_resource_identity(workspace_id, listing_id)`——价格与发布都作用于同一个
  Listing。`operation_intent` 走写路径，`resource_identity` 走回查路径。两个能力的
  指纹**长度相同但取值域不相交**（发布用 `ListingStatus` 名，价格用三位币种），因此
  跨能力复用同一个 `operation_id` 永远是冲突，不会误回放。
- **Mock Adapter**：`_OperationRecord` 增加 `applied_publication`；
  `_apply_publication` 在 `listing.status is target_state` 时直接返回
  `APPLIED + changed=False + 0 逻辑写`（不 bump 版本）；`_reconcile_listing` 对
  `resource_identity` 不符或 `applied_publication != command.expected_state.value`
  fail-closed。
- **测试模块命名坑**：pytest `prepend` 导入模式下同目录下的两个测试包不能共用
  `_support.py` 这个 basename（按 basename 缓存模块会 ImportError）。listing 的共享
  夹具改名为 `publication_support.py`（并在 docstring 里记录原因）。
- **门禁结果（PostgreSQL 由 `docker compose up -d postgres` 提供，容器 healthy）**：
  - 聚焦 `tests/agents/product_operations/listing`：**51 passed**；
  - 聚焦 `tests/agents/product_operations tests/adapters/commerce`：**328 passed**；
  - 全量 `pytest -q`：**1078 passed**（较 Task 7.2 的 1036 增加 42）；
  - `ruff check .`：通过；
  - `mypy runtime agents adapters apps eval`：244 files，通过；
  - `mypy tests/agents/product_operations/listing`：通过。
- **说明**：`ruff format --check` 全仓 234 个文件"would be reformatted"是既有的、
  未被门禁的基线，本轮未纳入；行内换行风格与相邻 inventory/pricing 测试保持一致。

---

### Task 8.1: 回查绑定持久化 Workspace + 幂等指纹显式能力前缀

Task 8 复核提出 1 个 P1、1 个 P2，均与"安全依赖隐含条件"有关，先补收口再进入 Task 9。

**Files:**
- Modify: `agents/product_operations/shared/reconciliation.py`
- Modify: `agents/product_operations/pricing/reconciliation.py`
- Modify: `agents/product_operations/inventory/reconciliation.py`
- Modify: `agents/product_operations/listing/reconciliation.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Modify: `tests/agents/product_operations/test_shared_contracts.py`
- Modify: `tests/agents/product_operations/pricing/test_price_reconciliation.py`
- Modify: `tests/agents/product_operations/inventory/test_inventory_recovery.py`
- Modify: `tests/agents/product_operations/listing/test_publication_recovery.py`
- Modify: `tests/adapters/commerce/test_mock_adapter.py`

- [x] **Step 1（P1）: 回查命令必须绑定持久化 Workspace**

三个 `build_reconcile_*_command` 原先把 `expected_state["workspace_id"]` 列入必需字段，
却在构造命令时**直接采用调用方传入的 `workspace_id`**——持久化 Workspace 被忽略。
错误装配或跨 Workspace 脏数据因此会带进平台回查，而不是 fail closed。

收口到 `shared/reconciliation.py` 的单一辅助函数：

```python
def resolve_persisted_workspace_id(
    expected_state: Mapping[str, Any], *, current_workspace_id: uuid.UUID
) -> uuid.UUID:
    # 缺失 / 无法解析 / 与 current_workspace_id 不一致 → VALIDATION_FAILED
```

三个 Reconciler 一律先解析持久化 `workspace_id`，与当前 Workspace 不一致即
`VALIDATION_FAILED`，命中后命令携带**已校验的**持久化身份。错误装配与跨 Workspace
脏数据一律 fail closed，不再"用当前 Workspace 覆盖持久预期"。

- [x] **P1 反例（修复前构造成功，修复后拒绝）**

```python
def test_build_reconcile_*_command_rejects_workspace_mismatch() -> None:
    expected_state = _expected_state()          # workspace_id = A
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_*_command(..., workspace_id=uuid.uuid4())   # 传 B
    assert exc.value.code == VALIDATION_FAILED
```

同一反例在 Pricing / Inventory / Listing 三个能力各锁定一条；`shared` 层另有
匹配 / 不一致 / 缺失 / 不可解析四个分支的直测。

- [x] **Step 2（P2）: 幂等指纹加入显式能力前缀**

`operation_intent` 原先**没有显式能力标识**。改价与刊登同作用于一个 Listing，指纹
中段同形（`external_store_ref`、`external_product_ref`），跨能力碰撞之所以不发生，
只是因为后续字段取值域恰好不相交（`target_price`↔`target_state`、`currency`↔`reason`）——
这是类型取值带来的偶然安全，不是明确契约；计划文档此前写的"`ListingStatus` 与三位
币种比较"也不准确。

现在三个指纹首元素是本能力的受控 `operation_type`（与 agents 的 `OPERATION_TYPE`
常量同源，避免漂移）：

```python
_PRICE        = "SET_LISTING_PRICE"
_INVENTORY    = "ADJUST_INVENTORY"
_PUBLICATION  = "CHANGE_LISTING_PUBLICATION"
```

跨能力复用同一 `operation_id` 因此在**结构上**即冲突，与后续字段的取值域、
元组长度都无关。

- [x] **P2 回归测试（同一 Listing、同一 operation_id，先改价再改发布）**

```python
async def test_reused_operation_id_across_capabilities_price_then_publication(...):
    await adapter.set_price(gateway_price_command(listing, operation_id=op))   # APPLIED
    conflict = await adapter.unpublish_listing(gateway_publication_command(...))  # 同 op
    assert conflict.status is WriteStatus.FAILED
    assert conflict.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await adapter.count_logical_writes(op) == 1
    assert (await adapter.get_listing(publication_query(listing))).publication_state \
        is ListingStatus.PUBLISHED
```

反向（先取消发布再改价）同样锁定，并断言价格未被改动。

- [x] **Step 3: 门禁**

- 聚焦 `tests/agents/product_operations tests/adapters/commerce`：**337 passed**；
- 全量 `pytest -q`：**1087 passed**（较 Task 8 的 1078 增加 9）；
- `ruff check .`：通过；
- `mypy runtime agents adapters apps eval`：244 files，通过；
- 改动测试文件 mypy：通过（`tests/adapters/commerce` 单独运行仅剩 2 条既有的
  `no-untyped-def`，位于本轮未触碰的 `test_rest_adapter.py`/`test_pricing_gateway.py`）。
- **既有测试修正**：`test_build_reconcile_listing_command_rejects_unknown_target_state`
  原本传入随机 `workspace_id`，P1 收口后会在"Workspace 不一致"处提前命中而掩盖
  目标状态解析分支；已改为传入匹配的 `workspace_id`，使其继续校验枚举解析。

---

### Task 8.2: 用结构断言锁定幂等能力命名空间

Task 8.1 新增的两条跨能力行为测试在旧实现上也会通过：旧价格指纹与刊登指纹虽然
没有能力前缀，但后续字段取值不同，仍然会被判为冲突。因此它们只能验证“当前发生
冲突”，不能证明“冲突由显式能力命名空间保证”。本任务只补测试证明力，不修改生产
实现。

**Files:**
- Modify: `tests/adapters/commerce/test_mock_adapter.py`

- [x] **Step 1: 写能够杀死旧实现的结构测试**

显式导入三个 intent 构造函数和对应 `OPERATION_TYPE`，基于测试命令断言首元素
（`product_seed` 是 async fixture，用例必须写成 `async def`）：

```python
from adapters.commerce.mock_adapter import (
    _inventory_operation_intent,
    _price_operation_intent,
    _publication_operation_intent,
)
from agents.product_operations.inventory.service import (
    OPERATION_TYPE as INVENTORY_OPERATION_TYPE,
)
from agents.product_operations.listing.service import (
    OPERATION_TYPE as LISTING_OPERATION_TYPE,
)
from agents.product_operations.pricing.service import (
    OPERATION_TYPE as PRICING_OPERATION_TYPE,
)


async def test_operation_intents_are_explicitly_namespaced_by_capability(
    product_seed: ProductOperationsSeed,
) -> None:
    listing = product_seed.listing
    price_intent = _price_operation_intent(
        gateway_price_command(listing, operation_id="op-price")
    )
    publication_intent = _publication_operation_intent(
        gateway_publication_command(listing, operation_id="op-listing"),
        ListingStatus.UNPUBLISHED,
    )
    inventory_intent = _inventory_operation_intent(
        AdjustInventoryCommand(
            operation_id="op-inventory",
            idempotency_key="adjust_inventory_quantity:op-inventory",
            workspace_id=product_seed.workspace.id,
            store_id=product_seed.store.id,
            location_id=product_seed.fulfillment_location.id,
            product_id=product_seed.product.id,
            external_store_ref=product_seed.store.external_store_ref,
            external_location_ref=(
                product_seed.fulfillment_location.external_location_ref
            ),
            external_product_ref=product_seed.listing.external_product_ref,
            sku=product_seed.product.sku,
            delta=1,
            reason="MANUAL",
            expected_version=product_seed.inventory_level.version,
            quantity_before=product_seed.inventory_level.available_quantity,
        )
    )

    assert price_intent[0] == PRICING_OPERATION_TYPE
    assert inventory_intent[0] == INVENTORY_OPERATION_TYPE
    assert publication_intent[0] == LISTING_OPERATION_TYPE
    assert len({price_intent[0], inventory_intent[0], publication_intent[0]}) == 3
```

- [x] **Step 2: 证明测试能识别旧实现**

使用 `git show f1e3427:adapters/commerce/mock_adapter.py` 读取旧函数，或在临时检验中移除
三个前缀；上述测试必须因首元素是 `workspace_id` 而失败。不得把“旧行为最终也返回
冲突”当作 RED 证据。

**实际 RED 证据**：临时删除三个指纹的首元素行后运行
`pytest -k "namespaced or across_capabilities"` →

```
FAILED ...::test_operation_intents_are_explicitly_namespaced_by_capability
E  assert '8455bff1-54cb-4bd9-b316-ef654a99e75b' == 'SET_LISTING_PRICE'
1 failed, 2 passed, 20 deselected
```

失败点是首元素为 `workspace_id`（UUID），而**两条跨能力行为测试在旧实现上仍然通过**
（`2 passed`）——正是本任务要解决的问题：行为测试只能证明“当前发生冲突”，不能证明
“冲突由显式能力命名空间保证”。随后 `git checkout -- adapters/commerce/mock_adapter.py`
恢复生产实现。

- [x] **Step 3: 恢复当前实现并运行聚焦测试**

```powershell
.venv\Scripts\python.exe -m pytest tests/adapters/commerce/test_mock_adapter.py -q
.venv\Scripts\python.exe -m ruff check tests/adapters/commerce/test_mock_adapter.py
```

Expected: 两条命令均通过。

**实际结果**：PASS。`23 passed in 2.58s`（旧 22 + 新增 1）；`ruff check` 通过；
`mypy tests/adapters/commerce/test_mock_adapter.py`（带 `MYPYPATH=tests`）成功。

- [x] **Step 4: 提交**

```powershell
git add tests/adapters/commerce/test_mock_adapter.py
git commit -m "test: lock idempotency capability namespaces"
```

**实际结果**：已提交（见下方 Task 8.2 提交说明）。

---

### Task 9: 建立 Adapter 能力契约测试

**Files:**
- Create: `tests/adapters/commerce/contracts.py`
- Modify: `tests/adapters/commerce/test_mock_adapter.py`
- Modify: `tests/adapters/commerce/test_rest_adapter.py`
- Modify: `adapters/commerce/rest_adapter.py`
- Modify: `adapters/commerce/__init__.py`

- [x] **Step 1: 提取共享契约测试**

`contracts.py` 提供三个可复用协程：`assert_price_gateway_contract`、`assert_inventory_gateway_contract`、`assert_listing_gateway_contract`。每个协程接收 Gateway 和对应 Fixture，并顺序执行成功写入、旧版本冲突、同 `operation_id` 重复提交、写后丢响应、按该 `operation_id` 回查五个断言；每一步都断言最终资源状态和逻辑写次数。

**实际实现**：契约参数化为 `PriceContractEnv` / `InventoryContractEnv` /
`ListingContractEnv`（frozen dataclass）。它们把"命令怎么构造、权威状态怎么读、
逻辑写怎么数、响应怎么丢"全部留给测试侧，因此契约本身不 import 任何具体 Adapter。
三个协程共用同一套五步顺序，每步同时断言**回执**、**平台权威状态**与**逻辑写次数**。

两处与原始描述不同的实现决定（均为让契约本身站得住）：

1. **写前基线改为显式读取**：版本（以及库存的 `quantity_before`）不再由 Fixture 硬编码，
   而由协程开头的一次 `read_state()` 确定，再据以推导后续版本断言。硬编码版本会让
   "上一套契约推进了版本"悄悄污染下一套的基线。
2. **重复提交必须是同一条已冻结命令**：库存的业务意图指纹包含
   `expected_quantity_after`（由写前 `quantity_before` 推导），所以"重读后再提交"会
   改变意图、被判 `IDEMPOTENCY_KEY_REUSED`。协程改为重放**同一条**命令，与两阶段
   协议"重试 `execute_prepared`"的真实链路一致。
3. 刊登契约的两个方向**交替**使用（初始状态 → 另一侧 → 回到初始状态）：同目标写入是
   幂等 no-op 且不推进版本，只有真正的状态迁移才能被"版本 +1 / 零逻辑写"观察到。
   步骤 2 的冲突方向必须回到初始状态，否则会先撞上"同目标 no-op"而不是 CAS。

- [x] **Step 2: Mock Adapter 跑三套契约**

```python
async def test_mock_adapter_satisfies_all_supported_contracts(mock_adapter, fixture) -> None:
    await assert_price_gateway_contract(mock_adapter, fixture.price)
    await assert_inventory_gateway_contract(mock_adapter, fixture.inventory)
    await assert_listing_gateway_contract(mock_adapter, fixture.listing)
```

**实际实现**：`test_mock_adapter_satisfies_all_supported_contracts` 用一个 Adapter
实例依次跑完三套契约，但**每个能力使用独立种子**（`product_seeder.seed()` 各调一次），
避免上一套契约推进的版本污染下一套的写前基线。

**RED 证据（证明契约有证明力）**：把 `_reconcile_price` 中"记录不存在或未提交"的
分支临时改成返回 `APPLIED`，契约在第 5 步 `contracts.py:191` 失败
（`never-seen operation_id` 必须 `NOT_APPLIED`）；恢复实现后通过。

- [x] **Step 3: REST Adapter 明确能力范围**

P1 的 REST Adapter 只声明 `pricing` 能力。库存和 Listing 方法若被误调用，返回结构化 `CAPABILITY_UNSUPPORTED`，不构造猜测性 URL。不要为了契约测试虚构第三方库存或刊登端点。

**实际实现**：`adapters/commerce/rest_adapter.py` 新增
`CAPABILITIES = frozenset({"pricing"})` 与 `CapabilityUnsupportedError`
（`LookupError` 家族，`reason_code` 固定为 `CAPABILITY_UNSUPPORTED`），类上声明
`supported_capabilities`。两类失败通道按 Port 语义分开：

- **读**抛 `CapabilityUnsupportedError`——与 `InventoryUnavailable` /
  `ListingUnavailable` 同属 `LookupError`，读失败通道不变；
- **写**返回 `FAILED` + `CAPABILITY_UNSUPPORTED` 回执（零观测）——写路径的确定失败
  本就以回执承载，且天然"绝不重试"；
- **回查**返回 `NOT_APPLIED`（能力不支持的写不可能生效），不发起任何请求。

`TYPE_CHECKING` 块追加三个 Port 的结构一致性断言：声明"能力范围"不等于"接口形状
可以不合"。`adapters/commerce/__init__.py` 导出 `CAPABILITIES` /
`CapabilityUnsupportedError` / `RestCommerceAdapter`。

测试 `test_unsupported_capabilities_fail_closed_without_network` 用
`AssertionError` 型 handler 保证九个方法**一次请求都没发出**。

- [x] **Step 4: 运行 Adapter 测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/adapters/commerce -q
git add adapters/commerce tests/adapters/commerce
git commit -m "test: add commerce gateway contract suites"
```

Expected: PASS，提交成功。

**实际结果**：PASS。`tests/adapters/commerce` → `42 passed in 3.58s`
（旧 41 + 新增 1）；`ruff check .` 通过；`mypy runtime agents adapters apps eval`
244 files 通过。`mypy tests/adapters/commerce` 仅剩 **2 条既有的** `no-untyped-def`
（`test_rest_adapter.py:59` 的 `make_adapter(handler, **kwargs)`、
`test_pricing_gateway.py:152`），均在本任务的改动区域之外，未纳入。

---

### Task 10: 将 PricePolicyConfig 接入正式 Workspace 配置

**Files:**
- Create: `agents/product_operations/pricing/configuration.py`
- Modify: `agents/product_operations/pricing/policy.py`
- Modify: `agents/product_operations/pricing/service.py`
- Modify: `agents/product_operations/policy/models.py`
- Modify: `agents/product_operations/policy/price_pretool.py`
- Modify: `composition/provisioning/product_operations.py`
- Modify: `composition/product_operations.py`
- Modify: `runtime/capabilities/run_factory.py`
- Modify: `runtime/persistence/repositories/run_snapshots.py`
- Modify: `runtime/execution/pretool/decisions.py`
- Modify: `runtime/execution/pretool/pipeline.py`
- Modify: `runtime/orchestration/agent_loop.py`
- Modify: `apps/worker/e2e.py`
- Modify: `eval/fixtures.py`
- Modify: `tests/agents/product_operations/conftest.py`
- Create: `tests/agents/product_operations/pricing/test_configuration.py`
- Modify: `tests/composition/test_product_operations_provisioning.py`
- Modify: `tests/runtime/capabilities/conftest.py`
- Modify: `tests/runtime/capabilities/test_assembly.py`
- Modify: `tests/runtime/execution/test_pretool_pipeline.py`
- Modify: `tests/runtime/orchestration/test_agent_loop.py`

- [x] **Step 1: 写“缺少已发布价格策略即装配失败”测试**

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

- [x] **Step 2: 实现价格策略发布和加载**

stable key 固定为 `product-pricing`。Provisioner 接收显式 `PricePolicyConfig`，把
Decimal 规范化成字符串后发布到已有 `BusinessPolicy` / `BusinessPolicyVersion`；相同
规范化配置幂等复用已发布版本，配置变化才递增版本。`ProductOperationsCapabilities`
在本任务新增 `pricing_policy_version_id` 与 `pricing_policy_config_hash`，不得等到
Task 11 才临时补字段。

```python
@dataclass(frozen=True)
class PublishedPricingPolicy:
    version_id: UUID
    version: int
    config: PricePolicyConfig
    config_hash: str
```

`load_published_pricing_policy()` 必须同时校验 Workspace、stable key、
`PUBLISHED` 状态、`config_hash` 与 Pydantic 配置形状；任一不一致都以
`PRICING_POLICY_NOT_PUBLISHED` / `PRICING_POLICY_CONFIG_INVALID` fail closed。

- [x] **Step 3: Run 固定 PolicySnapshot**

补齐当前存在但尚未接线的 `RunPolicySnapshot` 持久化能力：

- `RunSnapshotRepository.replace()` 接收通用的 policy pins，并与 Tool/Skill/Source
  快照在同一事务整体替换；增加按 `run_id` 读取 policy snapshot 的方法；
- `RunFactory` 接收 Runtime 通用值对象 `PolicySnapshotPin(policy_key,
  version_id, config_hash)`，不得 import 商品模块；
- `ProductApplication.start()` 把已加载的 `product-pricing` 精确版本与 hash 传入
  `RunFactory`；
- `tests/runtime/capabilities/test_assembly.py` 直接查询 `RunPolicySnapshot`，证明
  Run 创建后行已落库、重复 replace 不增行、其它 Run/Workspace 不受影响。

本任务**不新建迁移**：`run_policy_snapshots`、复合外键和
`business_policy_versions` 已存在；如果实现者发现模型与数据库不一致，应先验证现有
Alembic 历史，禁止再建同义表。

- [x] **Step 4: 把策略修订绑定到确认与写前状态**

`RunPolicySnapshot` 只解决审计，不会自动进入确认指纹。为避免“策略更新后仍接受旧确认”：

- Runtime `PolicyDecision` 新增可选的通用 `policy_revision: int | None`，
  `PreToolContext` 保留允许决策的 revision，`ConfirmationSpec.policy_version` 使用该值；
- `AgentLoop._handle_require_confirmation()` 把 `ConfirmationSpec.policy_version` 传入
  `ConfirmationService.create()`；验收必须证明 revision 变化后旧确认失效；
- Pricing 的 `PublishedPricingPolicy` 同时注入 PreTool policy 与执行 Handler；
  `PreparedSetPrice.expected_state()` 记录 `policy_version_id`、`policy_revision` 和
  `policy_config_hash`，使 Operation 能解释写前究竟校验了哪份规则；
- Runtime 只搬运 revision，不理解价格字段或 `product-pricing` stable key。

- [x] **Step 5: 删除正式装配路径中的 `price_config` 常量注入**

测试/Eval/E2E 通过显式 Provisioner 参数创建策略版本；所有现有调用点
（`tests/agents/product_operations/conftest.py`、`eval/fixtures.py`、
`apps/worker/e2e.py`）必须把原来的 `PricePolicyConfig` 改传给
`ProductOperationsProvisioner.provision(..., pricing_policy=...)`。正式
`build_product_application` 删除 `price_config` 参数，只根据
`capabilities.pricing_policy_version_id` 读取一次已发布配置，再把同一个
`PublishedPricingPolicy` 注入 PreTool 与 Handler。不得从 Prompt、环境变量或模块常量
回退配置。

- [x] **Step 6: 运行配置与 Runtime 快照测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations tests/composition/test_product_operations_provisioning.py tests/runtime/capabilities/test_assembly.py tests/runtime/execution/test_pretool_pipeline.py tests/runtime/orchestration/test_agent_loop.py tests/apps/api/test_e2e_product_flow.py tests/apps/worker/test_worker_lifecycle.py -q
git add agents/product_operations/pricing/configuration.py agents/product_operations/pricing/policy.py agents/product_operations/pricing/service.py agents/product_operations/policy/models.py agents/product_operations/policy/price_pretool.py composition/provisioning/product_operations.py composition/product_operations.py runtime/capabilities/run_factory.py runtime/persistence/repositories/run_snapshots.py runtime/execution/pretool/decisions.py runtime/execution/pretool/pipeline.py runtime/orchestration/agent_loop.py apps/worker/e2e.py eval/fixtures.py tests/agents/product_operations/conftest.py tests/agents/product_operations/pricing/test_configuration.py tests/composition/test_product_operations_provisioning.py tests/runtime/capabilities/conftest.py tests/runtime/capabilities/test_assembly.py tests/runtime/execution/test_pretool_pipeline.py tests/runtime/orchestration/test_agent_loop.py
git commit -m "feat: publish workspace pricing policy configuration"
```

Expected: PASS；装配缺少/错 Workspace/非发布/坏 hash 的策略均失败闭合；Run 的
PolicySnapshot 可查询；旧确认在策略 revision 变化后不可复用；提交成功。

**实际实现**（与计划的差异均已记录）：

1. **发布载荷修复了一个真实缺陷**：`PricePolicyConfig` 不做 Decimal 尺度归一，
   `Decimal("50.00") == Decimal("50.000")` 为真但 `str()` 不同——直接落库会让
   "数值相同的配置"算出不同 hash，幂等发布失效。`canonical_pricing_policy_payload`
   经 `_canonical_decimal_text`（`normalize()` + 定点格式）渲染成尺度无关文本。
   RED 证据：把归一化临时退回 `str(value)`，`test_configuration.py` 的尺度无关
   断言变红；恢复后通过。
2. **policy revision 进确认指纹**：`ConfirmationService.verify()` 把调用方传入的
   **当前** revision 与存储指纹重算比对（`_Unset` 哨兵区分"未提供"与"显式 None"），
   revision 变化即 `CONFIRMATION_STALE`；`AcceptedConfirmationLookup` 协议与组合根
   实现同步扩展 `policy_version` 参数；`PreToolContext` 保留允许决策的 revision 供
   确认阶段复用。RED 证据：临时让 verify 忽略当前 revision，新增漂移测试变红。
3. **测试改动**：`tests/runtime/orchestration/test_agent_loop.py`、
   `agents/product_operations/pricing/policy.py`、`policy/models.py` 无需改动
   （revision 经既有 `ConfirmationSpec` 透传，`PricePolicyConfig` 本身不动）；
   新增 `tests/agents/product_operations/pricing/test_configuration.py`、
   `test_assembly.py` 的 `test_run_pins_published_policy_versions`、组合层 6 个
   策略发布/加载用例，以及管线层 revision 透传用例。
4. **修复合并运行的跨套件污染**：组合层 provisioning 会提交全局能力目录行
   （`agents.stable_key` 全局唯一）且不回收，capabilities 套件随后种子同名行即撞
   `uq_agents_stable_key`。`tests/runtime/capabilities/conftest.py` 的
   `capability_catalog` 改为插入前先删同名残留（agent/skill/tool 及其版本行），
   删除在本 Session 事务内、随 rollback 还原。RED 证据：修复前三测试在合并运行
   ERROR（`UniqueViolationError`），修复后同组合全绿。
5. `.gitignore` 补 `.workbuddy/`、`docs/审计/`、`reports/`（协议规定永不入库，
   此前未 ignore 导致 ruff 扫到备份目录）。

**实际结果**：Task 10 门禁
`tests/agents/product_operations + tests/composition/test_product_operations_provisioning.py
+ tests/runtime/capabilities/test_assembly.py + tests/runtime/execution/test_pretool_pipeline.py
+ tests/runtime/orchestration/test_agent_loop.py + tests/runtime/interaction
+ tests/apps/api/test_e2e_product_flow.py + tests/apps/worker/test_worker_lifecycle.py`
→ **423 passed**；`ruff check .` 通过；`mypy runtime agents adapters apps eval
composition` → **249 source files 零错误**。

> 执行中断说明：Task 10 门禁通过前本机 `.git` 曾因误删进入回收站而损坏
> （packfile + refs 丢失）。已从回收站恢复全部对象（SHA1 逐一校验），两条中断的
> 提交链用 `git replace --graft` 重新接通（`62aa521`→`f1e3427`、`b491561`→`564b458`，
> 完全可逆）；3 个 tip 期被改文件的 blob 中 2 个按编辑记录逐字节重建并通过 SHA
> 校验，`tests/agents/product_operations/conftest.py` 的 tip 版 blob 无法唯一复原
> （不影响任何后续提交与新树，reflog 已归档至 `.workbuddy/backup-20261014/`）。
> 另：门禁期间 Postgres 容器未随会话恢复，已重启并等待 healthy 后重跑。

---

### Task 11: 发布 ProductOperations Agent 与兼容版本

执行本任务前必须完整阅读
`docs/架构/ADR-001-Skill能力模型.md`。本任务只发布与新 Tool 集合匹配的兼容
`SkillVersion`，不得把 ADR 中尚未实施的目标设计描述为当前事实，也不得删除
`required_skill_keys`、强制迁移 Skill 加载模型或修改已经固化的简历正文。

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
    pricing_policy_config_hash: str
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

- [x] **Step 1: 写组合根边界失败测试**

```python
def test_composition_has_no_domain_dispatch_or_policy_classes() -> None:
    source = Path("composition/product_operations.py").read_text(encoding="utf-8")
    assert "class _PriceBusinessPolicy" not in source
    assert "if key == STABLE_KEY" not in source
    assert "ReconciliationCommand(" not in source
```

- [x] **Step 2: 建立声明式 Binding 工厂**

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

- [x] **Step 3: 建立恢复应用服务**

`product_operations_recovery.py` 从 Operation 读取 `expected_state.operation_type`，经 `OperationReconcilerRegistry` 查找 Reconciler，再把结果交给 `Runtime ReconciliationService.apply_result()`。

- [x] **Step 4: 收缩 `composition/product_operations.py`**

最终只保留：读取已发布快照、创建 Adapter/Service/Registry/Runtime、连接依赖、返回 `ProductOperationsApplication`。删除价格命令转换、读 Tool 分发和领域回查判断。

- [x] **Step 5: 运行组合与架构测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/composition tests/architecture/test_dependency_boundaries.py -q
git add composition/product_operations.py composition/product_operations_bindings.py composition/product_operations_recovery.py tests/composition tests/architecture/test_dependency_boundaries.py
git commit -m "refactor: reduce product operations composition root"
```

Expected: PASS，提交成功。

实际验收：商品域、组合层与架构边界测试共 **340 passed**；同时把仍通过新
Agent 发起旧 Tool 的 Runtime 路径测试迁移到 `store_ref` / `store_refs` 与
`set_listing_price` / `get_listing`，未把 compatibility Tool 重新暴露给新 Run。

---

### Task 13: 建立 EvalCapabilityManifest 并迁移评测协议

**Files:**
- Create: `eval/capability_manifest.py`
- Modify: `eval/config.py`
- Modify: `eval/fixtures.py`
- Modify: `eval/runner.py`
- Modify: `eval/schema.py`
- Modify: `eval/product_suite.py`
- Modify: `eval/reporting/json_report.py`
- Modify: `eval/reporting/markdown_report.py`
- Modify: `eval/datasets/product_operations/*.yaml`
- Modify: `eval/datasets/product_operations/manifest.json`
- Modify: `tests/eval/conftest.py`
- Create: `tests/eval/test_capability_manifest.py`
- Modify: `tests/eval/test_schema.py`
- Modify: `tests/eval/test_dataset_partitions.py`
- Modify: `tests/eval/test_runner_isolation.py`
- Modify: `tests/eval/test_reporting.py`

- [ ] **Step 1: 写版本冻结失败测试**

```python
async def test_eval_manifest_is_frozen_before_cases_run(eval_fixture) -> None:
    manifest = await build_eval_capability_manifest(eval_fixture.capabilities)
    await eval_fixture.publish_new_tool_version("set_listing_price")
    case = await eval_fixture.create_case_environment(manifest=manifest)
    assert case.capability_fingerprint == manifest.capability_fingerprint
```

再加一条反例：隔离环境实际发布的 Tool/Policy hash 与 manifest 不一致时，必须在模型
调用前返回 `CAPABILITY_MANIFEST_MISMATCH`；不得静默改用“当前最新版”。

- [x] **Step 2: 定义 EvalCapabilityManifest**（实际结果：`eval/capability_manifest.py`，stable key/version/hash 逻辑身份，fingerprint 为 canonical JSON SHA-256）

Eval 每个 case/repetition 都创建独立 Workspace，Workspace 级 Model/Policy Version ID
天然不同；因此 Manifest 冻结的是**逻辑能力身份**（stable key + version + 内容 hash），
而不是把某个临时 Workspace 的 UUID 错当成跨 case 身份。Run 内仍继续使用真实 Version
ID 快照，两层语义不能混淆。

```python
class CapabilityVersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stable_key: str
    version: int
    content_hash: str


class EvalCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str
    agent: CapabilityVersionRef
    prompt_version: str
    skills: dict[str, CapabilityVersionRef]
    tools: dict[str, CapabilityVersionRef]
    model_snapshot: dict[str, JsonValue]
    policies: dict[str, CapabilityVersionRef]
    dataset_hashes: dict[str, str]
    scorer_version: str
    capability_fingerprint: str
```

`content_hash` 对 Tool 使用 schema/implementation 契约 hash，对 Agent/Skill 使用已发布
内容 hash，对 Policy 使用 `config_hash`。`dataset_hashes` 固定五个分区；
`capability_fingerprint` 是除自身外全部字段的 canonical JSON SHA-256。

- [x] **Step 3: 删除 Eval 中复制的能力版本常量**（实际结果：`_FIXED_*` 四组常量全部删除，schema fingerprint 只从 Manifest 派生）

删除 `_FIXED_AGENT_VERSION`、`_FIXED_PROMPT_VERSION`、`_FIXED_SKILL_VERSIONS`、
`_FIXED_TOOL_VERSIONS`。`EvalConfig` 只保留 Provider、采样参数、repetitions 与
scorer version；`eval/schema.py` 的 system/protocol fingerprint 显式接收 Manifest，
不得继续从 `EvalConfig` 读取一份重复的能力版本。Provider 配置仍只负责模型连接和固定
采样参数；Manifest 由 Provisioner 返回的已发布行与数据集文件派生。

- [x] **Step 4: 让隔离环境验证 Manifest，而不是重新选择最新版**（实际结果：`EvalRunner` 整轮接收单一不可变 Manifest，`IsolatedEnvironment.create()` 按 stable key/version/hash 校验，期间发布新版本不影响进行中的轮次）

`EvalRunner` 在整轮开始前只接收一个不可变 Manifest，并传给每个
`IsolatedEnvironment.create()`。每个隔离 Workspace 仍独立发布自己的 Model/Policy
行，但必须校验 stable key、version、hash 与 Manifest 一致；Agent/Skill/Tool 也按
Manifest 指定版本解析。任何新版本在评测期间发布，都不能改变已开始的这一轮。

`IsolatedEnvironment.cleanup()` 可以删除 case 自己的 Workspace 级行，但不得让下一
case 因重新生成 UUID 而改变 Manifest；全局能力的比较以 stable key/version/hash 为准，
并保持现有数据库隔离与清理能力。

- [ ] **Step 5: 更新数据集 Tool 名称但不更新成绩**

将用例中的旧 Tool 断言迁移到八个新 stable key；库存和刊登各增加核心正常任务、安全拒绝和恢复案例。更新 dataset hash。不要在本任务生成或手工修改任务完成率。

- [ ] **Step 6: 提供一次生成、五次复用的 Manifest 文件入口**

`eval.capability_manifest` 提供 CLI，把最终发布能力与五个数据集 hash 解析成一个 JSON
文件；`eval.product_suite` 新增必填 `--capability-manifest`，只读取该文件并在任何模型
请求前验证 fingerprint 与当前隔离环境。五个 split 不允许各自重新生成 Manifest。

CLI 在临时 Eval Workspace 中调用正式 `ProductOperationsProvisioner`，从已发布数据库行
读取 version/hash 后生成 Manifest，随后按外键逆序清理该临时 Workspace；不能直接从
Python 常量拼一份“看起来一致”的 JSON。临时 Workspace/Version UUID 不进入 Manifest。

```powershell
.venv\Scripts\python.exe -m eval.capability_manifest --output reports/product-operations-p1-manifest.json
```

Manifest 文件不含 API Key、数据库 URL、内部 Actor/Workspace ID 或临时 Version UUID。

- [ ] **Step 7: 报告写入 Manifest**

JSON 和 Markdown 报告必须记录完整 manifest 与协议指纹，确保两次成绩可以追溯到同一能力快照。

- [x] **Step 8: 运行 Eval 单元测试并提交**

```powershell
.venv\Scripts\python.exe -m pytest tests/eval -q
git add eval tests/eval
git commit -m "refactor: freeze eval capability manifest"
```

Expected: PASS，提交成功。

实际结果：tests/eval 144 passed（含 `test_capability_manifest.py` 7 项）；ruff 通过；mypy 253 源文件零错误。

---

### Task 14: 完成端到端闭环、文档收口、全量验收和真实模型复测

**Files:**
- Modify: `tests/apps/api/test_e2e_product_flow.py`
- Modify: `tests/apps/worker/test_worker_lifecycle.py`
- Create: `tests/agents/product_operations/test_p1_safety_matrix.py`
- Modify: `docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md`
- Modify: `README.md`

- [x] **Step 1: 增加四条端到端主路径**（实际结果：`tests/apps/api/test_e2e_product_flow.py` 六条用例——价格主路径 CONFIRM 模式（含缺店铺 Input + 确认参数断言）、价格全路径 AUTO 模式（搜索→比价→改价→回查）、库存路径 CONFIRM/AUTO、发布路径 CONFIRM、下架路径 AUTO；脚本后端 `E2EScriptedBackend` 扩展为任务表驱动的多步引擎；种子补充库存/位置/三类写权限

覆盖：

```text
搜索商品 → 比价 → 设置 Listing 价格 → 回查
读取库存 → 增量调整 → 回查
读取 Listing → 发布 → 回查
读取 Listing → 取消发布 → 回查
```

每条写路径同时验证 `AUTO_EXECUTE` 与 `REQUIRE_CONFIRMATION`，确认后必须重新校验权限、Policy 和资源版本。

- [x] **Step 2: 增加安全矩阵**（实际结果：`tests/agents/product_operations/test_p1_safety_matrix.py` 27 项——4 写工具 × 6 场景（无写权限/跨店铺/空资源范围/确认后版本变化/响应丢失/重复 operation_id）+ REST Adapter 能力边界 3 项；确认后版本变化统一为 DENIED + LISTING_VERSION_CONFLICT（通用资源版本守卫语义，已记入 ADR-002）

对四个写 Tool 参数化验证：无写权限、跨店铺、缺少关键范围、确认后版本变化、响应丢失、重复 operation_id、Adapter 不支持能力。

- [x] **Step 3: 运行针对性测试**（实际结果：416 passed）

```powershell
.venv\Scripts\python.exe -m pytest tests/agents/product_operations tests/adapters/commerce tests/composition tests/apps/api/test_e2e_product_flow.py tests/apps/worker/test_worker_lifecycle.py tests/runtime/integration/test_crash_recovery.py -q
```

Expected: PASS。

- [x] **Step 4: 运行全量质量门禁**（实际结果：pytest 全量 1157 passed；ruff 通过；mypy 253 源文件 0 错误）

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

- [x] **Step 5: 检查旧命名残留**（实际结果：命中仅在 legacy 常量（`LEGACY_AGENT_KEY`）、兼容版本发布（`legacy_product_tool_definitions`）、旧工具模块本体与架构/设计文档的 legacy/历史段；workbench 前端测试的普通工具名已改 `set_listing_price`，`TOOL_UNKNOWN` 用例保留旧名（语义即旧工具被拒）；前端 Vitest 30 passed

```powershell
rg -n "ProductMaintenanceAgent|product-maintenance|get_product_snapshot|update_price" agents composition apps eval README.md docs/架构
```

Expected: 命中只允许出现在明确标注的 legacy compatibility、历史迁移说明和 ADR 当前事实段；新 Agent、Prompt、Eval 数据集和普通装配路径不得命中。

- [x] **Step 6: 更新架构事实**（实际结果：ADR-002 状态改为"P0/P1 已实施，P2 待实施"并补 P1 实施记录（提交序列 + 门禁结果 + 实施差异）；README 更新为四能力模块事实描述与最新验证数字）

只有全部门禁通过后，才将 ADR-002 状态改为“P0/P1 已实施，P2 待实施”，记录实际提交号、最终测试数量和验证命令。README 只描述已实现能力，不宣称接入真实第三方平台。

- [ ] **Step 7: 基于冻结 Manifest 运行真实模型复测**（待办：缺少 `EVAL_API_KEY` 等环境变量，按协议暂停等待凭据）

先检查三个环境变量，只允许从当前进程环境读取 Secret，不把 Key 写入仓库、命令历史、
报告或日志：

```powershell
if (-not $env:EVAL_API_KEY) { throw "缺少 EVAL_API_KEY" }
$env:EVAL_BASE_URL = "https://api.deepseek.com"
$env:EVAL_MODEL = "deepseek-flash"
```

使用 Task 13 生成并冻结的 `EvalCapabilityManifest` 依次运行五个分区；不得在运行期间
修改 Prompt、Skill、Tool Schema、数据集或 Scorer：

```powershell
.venv\Scripts\python.exe -m eval.capability_manifest --output reports/product-operations-p1-manifest.json
$manifest = "reports/product-operations-p1-manifest.json"
.venv\Scripts\python.exe -m eval.product_suite --split core --capability-manifest $manifest --output reports/product-operations-p1-core.json
.venv\Scripts\python.exe -m eval.product_suite --split clarification --capability-manifest $manifest --output reports/product-operations-p1-clarification.json
.venv\Scripts\python.exe -m eval.product_suite --split regression --capability-manifest $manifest --output reports/product-operations-p1-regression.json
.venv\Scripts\python.exe -m eval.product_suite --split safety --capability-manifest $manifest --output reports/product-operations-p1-safety.json
.venv\Scripts\python.exe -m eval.product_suite --split blind --capability-manifest $manifest --output reports/product-operations-p1-blind.json
```

判定规则：

- Provider 认证、网络或限流失败记为 `INFRASTRUCTURE_ERROR`，保留报告并在最终交付中
  单列，不计入任务完成率，也不得通过修改用例来“修复”；
- 产品用例失败保留真实分母和轨迹，禁止手工改分；
- 新报告使用新文件名，不覆盖 `docs/评测/首版报告.md` 或历史基线；
- 每份报告的 `capability_fingerprint` 必须与 `$manifest` 完全相同；只有五个分区都
  基于同一个 Manifest 完成，才允许比较新的任务完成率；安全分区单独报告越权拦截和
  误拒，不能混进业务任务完成率。

- [ ] **Step 8: 提交验收文档与测试**

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
- [ ] Runtime 写入重试预算上界生效：同一 Operation 的 `NOT_APPLIED` 重排不超过预算，
  预算耗尽后 ToolCall 落 `FAILED`（实现于 Runtime，Pricing 只做验收）；
- [ ] `unpublish_listing` 不删除 Product、Listing 或库存；
- [ ] PricePolicyConfig 来自已发布 Workspace 配置；
- [ ] Run 创建时持久化 `product-pricing` 的 Version ID 与 config hash，确认指纹绑定
  Policy revision，Operation.expected_state 可追溯该策略；
- [ ] Task 8.2 的结构测试能杀死“仅靠字段取值域不相交”的旧幂等实现；
- [ ] Eval 五个分区使用同一个 `EvalCapabilityManifest.capability_fingerprint`，隔离
  Workspace 通过 stable key/version/hash 校验真实能力，不伪冻结临时 UUID；
- [ ] Runtime 不导入 `agents.product_operations` 或 `adapters.commerce`；
- [ ] 组合根不包含领域规则和按 Tool 业务分支；
- [ ] Skill 只完成兼容版本更新，ADR-001 目标模型仍标记为未实施；
- [ ] 全量 pytest、ruff、mypy 通过；
- [ ] P1 完成后重新运行真实模型评测；若缺凭据/Provider 不可用，代码交付仍完成，但
  最终报告必须明确标记外部阻塞，不得编造或沿用旧成绩。
