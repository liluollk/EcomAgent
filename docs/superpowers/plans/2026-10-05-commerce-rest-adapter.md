# 商品平台 REST Adapter 与 Runtime 事务边界实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先修复 Runtime 在外部 I/O 期间持有数据库事务的问题，再实现专用 REST 商品 Adapter；用确定性测试验证状态原子性、租约 fencing、安全写入、幂等和不确定结果回查。

**Architecture:** `update_price` 仍是本地商品 Tool，Runtime 仍负责授权、确认、Operation 状态和恢复。RuntimeHost 不再持有覆盖整个 AgentLoop 的事务；AgentLoop 按持久化检查点提交状态与事件，所有模型/Tool/Adapter/回查网络等待都在事务外，写 Operation 的登记事务先于外部调用提交。复用现有 `WorkerHost` 续租与失租取消，Runtime 每个提交检查点执行 lease fencing。然后新增 `RestCommerceAdapter` 作为 `CommerceAdapter` 的第二种实现。MCP 继续由 Source 装载，不增加通用 REST Source，也不接入真实淘宝/抖音官方 API。

**Tech Stack:** Python 3.12、httpx、Pydantic、SQLAlchemy、pytest、MockTransport、PostgreSQL 测试库。

> 执行状态：实现与验证已完成；没有创建 Git 提交，改动保留在工作树供用户审阅。租约续期复用现有 WorkerHost，本次只补 Runtime 检查点 fencing。

---

## 文件职责图

| 文件 | 职责 |
|---|---|
| `adapters/commerce/protocol.py` | 商品写入命令、回查命令、回执和生产 Adapter 协议 |
| `adapters/commerce/rest_adapter.py` | 固定商品 REST 契约的 HTTP 映射、错误归一化和回查 |
| `adapters/commerce/mock_adapter.py` | 确定性数据库 Mock 实现；保留仅供测试的逻辑写计数方法 |
| `adapters/commerce/product_runtime.py` | 从持久化 ToolCall 构造精确的回查命令；通过依赖注入选择 Adapter |
| `eval/fixtures.py` | Eval 环境暴露独立的 Mock 写入观察器，不再从生产协议取测试计数 |
| `eval/runner.py`、`eval/recovery_suite.py` | 通过测试观察器统计 Mock 平台逻辑写，不改变评测语义 |
| `tests/adapters/commerce/test_rest_adapter.py` | REST 请求映射、HTTP 错误和回查协议测试 |
| `tests/adapters/commerce/test_mock_adapter.py`、`tests/agents/product_operations/test_price_flow.py`、`tests/agents/product_operations/test_safety_matrix.py` | 回归 Mock Adapter、商品写链路与安全矩阵 |
| `tests/eval/` | 验证 Eval 使用独立写入观察器且报告分母/副作用口径不变 |
| `docs/架构/Runtime核心接口与Harness设计.md`、`docs/架构/商品运营参考Agent设计.md`、`README.md` | 记录真实边界、协议语义和未完成的官方平台联调 |
| `runtime/orchestration/runtime_host.py` | 去掉全推进事务；提供绑定当前 lease 的事务检查点与最终提交 |
| `runtime/orchestration/agent_loop.py`、`runtime/orchestration/streaming.py` | AgentLoop 状态/事件短事务检查点；模型、读写 Tool 调用前确保事务已提交；流输出每个合并块提交并在恢复时关闭遗留 STREAMING 消息 |
| `runtime/execution/tool_executor.py`、`runtime/execution/reconciliation.py` | Operation begin/result 两阶段事务；回查结果 fenced 提交 |
| `agents/product_operations/tools/update_price.py`、`adapters/commerce/mock_adapter.py` | Listing 命令读取事务与 Adapter 调用分离；Mock 平台自己管理模拟平台写事务 |

## Task 0：拆分 Runtime 状态事务与外部调用边界

**Files:**
- Modify: `runtime/orchestration/runtime_host.py`
- Modify: `runtime/orchestration/agent_loop.py`
- Modify: `runtime/orchestration/streaming.py`
- Modify: `runtime/execution/tool_executor.py`
- Modify: `runtime/execution/reconciliation.py`
- Modify: `adapters/commerce/product_runtime.py`
- Modify: `agents/product_operations/tools/update_price.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Test: `tests/runtime/orchestration/test_agent_loop.py`
- Test: `tests/runtime/integration/test_crash_recovery.py`
- Test: `tests/runtime/jobs/test_leases.py`
- Test: `tests/apps/worker/test_worker_lifecycle.py`
- Test: `tests/agents/product_operations/test_price_flow.py`

- [x] **Step 1: 用阻塞 Fake Backend 暴露外部等待期间的活动事务**

在 RuntimeHost 测试中注入 Backend：`stream()` 等待一个测试闸门；闸门打开前由另一个 Session 成功更新同一 Run 相关记录，且执行 Session 的 `in_transaction()` 为 False。当前实现应失败，证明模型请求被外层 `session.begin()` 包住。另加断言：`Run` 状态迁移及对应 AgentEvent 提交/回滚一致。

```powershell
uv run pytest tests/runtime/orchestration/test_agent_loop.py -k transaction_boundary -q
```

预期：新增的“网络等待时无事务”测试失败；原有状态/事件原子性测试通过。

- [x] **Step 2: 去掉 RuntimeHost 的全推进事务并定义 AgentLoop 提交检查点**

`RuntimeHost.handle_lease` 只负责构造 Loop、调用 `advance`、对异常回滚未提交变更并关闭 Session。AgentLoop 在数据库状态转换及其事件已经完整写入后调用统一 checkpoint；RuntimeHost 注入的 checkpoint 在一个数据库事务中用 `ensure_write_fencing` 校验 lease token/有效期后提交。不得让每个 Repository 自行 commit。对仍未提交的中间异常状态执行 rollback，由 Job Worker 做可恢复错误处理。

检查并迁移所有返回路径：QUEUED→RUNNING、ToolCall 提议、PreToolUse 分流/挂起、写操作 begin/result、ToolResult 消息、Run 完成/失败与事件均保留正确原子边界。

- [x] **Step 3: 让模型流等待期间没有数据库事务**

模型请求发出前提交已准备状态。`StreamingMessageRecorder` 创建 assistant Message 后提交；每个内容合并块连同对应 AgentEvent 写入后立即 checkpoint，再向 Backend 请求下一事件；完成/中止终态同样短事务提交。Backend retry Attempt 必须在下次重试网络请求前提交。保留当前按阈值合并，不逐 token 写库。

Worker 在首块落库后崩溃时，恢复推进先把遗留 `STREAMING` Message 标为 `ABORTED`，再创建新的模型轮次；测试证明部分输出不进入上下文。

```powershell
uv run pytest tests/runtime/orchestration/test_agent_loop.py -k "stream or transaction_boundary" -q
```

- [x] **Step 4: 拆分写 Tool 执行两阶段事务并覆盖读 Tool**

`ToolExecutor` 将 `Operation.begin` 与 Attempt/audit 在独立短事务提交后再请求 handler；收到回执后在新事务完成 PostToolUse、Operation/ToolCall/Run 与事件。读 Tool 的状态变化在发请求前提交；回执处理/事件在返回后提交。新增阻塞 Read/Write Handler 测试，等待期间执行 Session 无活动事务；响应丢失时 Operation 已持久化为可回查事实。

```powershell
uv run pytest tests/runtime/orchestration/test_agent_loop.py tests/runtime/execution/test_operations.py -k "transaction or operation" -q
```

- [x] **Step 5: 加入租约续期与租约丢失停止条件**

复用 `apps/worker/main.py` 中已有的 WorkerHost heartbeat，不在 RuntimeHost 另建心跳。确认续租失败会取消 handler；Runtime 每次外部调用前与提交 checkpoint 前执行 fencing，已失租 Worker 不得产生后续模型/Tool 请求或提交 Run/Event 状态。外部写已发出而租约失效时，保留 `Operation(EXECUTING)`，交由恢复/回查判定，不盲写。使用现有 WorkerHost 生命周期测试与短租期 Fake 测试验证续租、取消与旧 token fencing。

```powershell
uv run pytest tests/runtime/jobs/test_leases.py tests/apps/worker/test_worker_lifecycle.py tests/runtime/integration/test_crash_recovery.py -k "heartbeat or lease or stale" -q
```

- [x] **Step 6: 拆分回查与结果提交**

商品 `recover` 先提交回查 Job 的 claim 与准确 Operation 上下文；`CommerceAdapter.reconcile` 在事务外执行；`ReconciliationService.apply_result` 在新的 fenced 事务提交 Attempt、审计、状态及事件。结果提交前崩溃允许重复 GET/回查；`NOT_APPLIED` 仍需回到完整 PreToolUse。使用阻塞 Reconcile Fake 验证无事务等待和续租。

- [x] **Step 7: 运行 Runtime 窄回归并保留未提交工作树**

```powershell
uv run pytest tests/runtime/orchestration tests/runtime/jobs tests/runtime/execution tests/runtime/integration/test_crash_recovery.py tests/agents/product_operations/test_price_flow.py -q
uv run mypy runtime agents adapters apps eval
```

预期：AgentLoop、租约、崩溃恢复与商品写入测试通过；状态/Event 原子性与无外部 I/O 长事务测试通过；mypy 通过。

本轮不创建提交，改动保留在工作树供用户审阅。

## Task 1：拆分生产 Adapter 合同与测试观察能力

**Files:**
- Modify: `adapters/commerce/protocol.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Modify: `eval/fixtures.py`
- Modify: `eval/runner.py`
- Modify: `eval/recovery_suite.py`
- Test: `tests/adapters/commerce/test_mock_adapter.py`
- Test: `tests/agents/product_operations/test_price_flow.py`
- Test: `tests/agents/product_operations/test_safety_matrix.py`
- Test: `tests/eval/`

- [x] **Step 1: 先写生产协议不暴露测试计数的测试**

在 `tests/adapters/commerce/test_mock_adapter.py` 增加断言：`CommerceAdapter` Protocol 不声明 `count_logical_writes`，但 `MockCommerceAdapter` 仍能提供该测试观测方法。执行命令：

```powershell
uv run pytest tests/adapters/commerce/test_mock_adapter.py -q
```

预期：新断言失败，指出生产 Protocol 仍包含测试计数能力。

- [x] **Step 2: 写 Eval 注入 Mock 写入观察器的失败测试**

为 `IsolatedEnvironment` 增加 `commerce_write_observer` 字段，测试构造环境时由同一 `MockCommerceAdapter` 实例提供此观察器；在 Eval 单测中断言 `runner` 和 `recovery_suite` 不通过 `environment.runtime.adapter` 读取写次数。执行命令：

```powershell
uv run pytest tests/eval -q
```

预期：测试因字段缺失或旧计数调用失败。

- [x] **Step 3: 从生产协议移除测试计数并接入独立观察器**

从 `CommerceAdapter` 删除 `count_logical_writes` 方法声明；保留 `MockCommerceAdapter.count_logical_writes` 作为 Mock 测试 API。Eval fixture 显式保存 `commerce_write_observer`，两个 Eval Runner 只通过该观察器计数。生产 Runtime 继续把 Adapter 注解为 `CommerceAdapter`。

- [x] **Step 4: 运行协议、商品写入与 Eval 回归**

```powershell
uv run pytest tests/adapters/commerce/test_mock_adapter.py tests/agents/product_operations/test_price_flow.py tests/agents/product_operations/test_safety_matrix.py tests/eval -q
uv run mypy runtime agents adapters apps eval
```

预期：相关测试全部通过；mypy 无 `CommerceAdapter` 缺少或多余成员错误；Eval 报告逻辑写分项与改动前相同。

- [x] **Step 5: 本轮不创建提交**

本轮不创建提交，改动保留在工作树供用户审阅。

## Task 2：让回查绑定到持久化的具体操作

**Files:**
- Modify: `adapters/commerce/protocol.py`
- Modify: `adapters/commerce/product_runtime.py`
- Modify: `adapters/commerce/mock_adapter.py`
- Test: `tests/adapters/commerce/test_mock_adapter.py`
- Test: `tests/agents/product_operations/test_price_flow.py`
- Test: `tests/runtime/integration/test_crash_recovery.py`

- [x] **Step 1: 写“同一 Run 有多个 ToolCall 时按 UNKNOWN Operation 精确回查”的失败测试**

创建两个不同 `ToolCall`，确保只有指定 Operation 为 `UNKNOWN`；恢复时断言 Adapter 收到与该 Operation 关联的店铺、Listing 和目标价格，而不是仅按 Run 内最新 ToolCall 猜测。执行：

```powershell
uv run pytest tests/agents/product_operations/test_price_flow.py -k reconcile -q
```

预期：测试失败，暴露当前 `_reconcile_once` 仅选 Run 中最新 ToolCall 的隐含假设。

- [x] **Step 2: 定义不可变回查命令并更新 Protocol**

在 `adapters/commerce/protocol.py` 增加 `ReconciliationCommand`，字段为 `operation_id`、`workspace_id`、`store_id`、`listing_id`、`external_store_ref`、`external_product_ref`、`expected_price` 和 `currency`。将协议改为：

```python
async def reconcile(self, command: ReconciliationCommand) -> ReconcileResult: ...
```

命令由数据库中的对应 `Operation` 与其 `tool_call_id` 关联的持久化 `ToolCall.normalized_arguments` 重建；不得由模型重述，也不得让 Adapter 查询 Runtime 数据库。

- [x] **Step 3: Runtime 按 Operation 关联 ToolCall 并重建回查命令**

在 `ProductApplication._reconcile_once` 查询当前 Run 中状态为待回查的 Operation，再按 `Operation.tool_call_id` 取 ToolCall；用已持久化的规范化参数解析商品与店铺，形成 `ReconciliationCommand`。若 Operation/ToolCall/Listing 缺失或 Workspace 不一致，返回确定的回查错误并保持 UNKNOWN，不尝试写入。

- [x] **Step 4: 迁移 Mock Adapter 与故障测试到新回查命令**

`MockCommerceAdapter.reconcile` 只使用命令字段定位记录和权威 Listing；更新响应丢失、重复提交、UNKNOWN 回查和崩溃恢复测试，保持“回查 `NOT_APPLIED` 后必须重新过完整 PreToolUse”的既有不变量。

- [x] **Step 5: 运行窄测试**

```powershell
uv run pytest tests/adapters/commerce/test_mock_adapter.py tests/agents/product_operations/test_price_flow.py tests/runtime/integration/test_crash_recovery.py -q
uv run mypy runtime agents adapters apps eval
```

预期：测试通过；未知 Operation 不会匹配到其他 ToolCall；mypy 通过。

- [x] **Step 6: 本轮不创建提交**

本轮不创建提交，改动保留在工作树供用户审阅。

## Task 3：实现固定 REST 商品协议 Adapter

**Files:**
- Create: `adapters/commerce/rest_adapter.py`
- Modify: `adapters/commerce/protocol.py`
- Modify: `adapters/commerce/product_runtime.py`
- Test: `tests/adapters/commerce/test_rest_adapter.py`
- Test: `tests/adapters/commerce/conftest.py`

- [x] **Step 1: 写 REST 成功请求映射测试**

使用 `httpx.MockTransport` 构造确定性 API：价格更新请求携带外部店铺/商品标识、币种、期望版本和 `Idempotency-Key`；成功响应映射为 `ExecutionReceipt(APPLIED, before_price, after_price, listing_version, external_ref)`。先运行：

```powershell
uv run pytest tests/adapters/commerce/test_rest_adapter.py -q
```

预期：因模块或 Adapter 尚不存在而失败。

- [x] **Step 2: 固定 REST 契约的数据形状**

更新端点使用 `PATCH /v1/stores/{external_store_ref}/listings/{external_product_ref}/price`，请求 JSON 为 `{"price": "<decimal>", "currency": "<ISO-4217>", "expected_version": <int>}`；携带 `Idempotency-Key: update_price:<operation_id>`。回查使用 `GET /v1/operations/{operation_id}`，并返回明确的 `APPLIED`、`NOT_APPLIED` 或 `UNKNOWN` 状态。

- [x] **Step 3: 实现 REST Adapter 最小路径**

新增 `RestCommerceAdapter`，构造时注入受信任配置中的 HTTPS `base_url`、主机 allowlist、不回显的凭证值和 `httpx.AsyncClient`。只实现 `execute` 与 `reconcile`：禁止动态 URL/方法；不在 Adapter 内重试写请求；解析 Decimal 和版本字段失败时返回 UNKNOWN，不猜成功。

- [x] **Step 4: 写并通过 HTTP 错误与安全测试**

覆盖 HTTP 409 版本冲突 → `FAILED`、明确 4xx 拒绝 → `FAILED`、5xx/连接超时/响应无法解析 → `UNKNOWN`、连接与读取超时上限、响应体大小上限、跨主机重定向拒绝、凭证不会进入异常/日志、操作查询 UNKNOWN 保持 UNKNOWN。HTTP client 禁止自动重试写请求。执行：

```powershell
uv run pytest tests/adapters/commerce/test_rest_adapter.py -q
```

预期：所有 REST 协议测试通过。

- [x] **Step 5: 验证恢复链路不发生盲重写**

Adapter 测试验证 PATCH 超时不会自动重写且同一 Operation 可以 GET 回查；独立的商品 Runtime 恢复测试验证 Mock 平台响应丢失后 Runtime 回查并且逻辑写次数为 1。当前没有声称这两项已组成真实 HTTP E2E。

```powershell
uv run pytest tests/agents/product_operations/test_price_flow.py -k rest_response_loss -q
```

- [x] **Step 6: 运行 Adapter 与商品端到端测试，不创建提交**

```powershell
uv run pytest tests/adapters/commerce tests/agents/product_operations -q
```

预期：窄测试通过；改动保留在工作树，没有创建提交。

## Task 4：组合根接入、文档同步与全量验证

**Files:**
- Modify: `adapters/commerce/product_runtime.py`
- Modify: `eval/fixtures.py`
- Modify: `docs/架构/Runtime核心接口与Harness设计.md`
- Modify: `docs/架构/商品运营参考Agent设计.md`
- Modify: `README.md`
- Test: `tests/adapters/commerce/test_rest_adapter.py`
- Test: `tests/agents/product_operations/`
- Test: `tests/eval/`

- [x] **Step 1: 写组合根选择 Adapter 的测试**

测试 `build_product_application` 可注入 `CommerceAdapter` 实例；未注入时 Eval/E2E 继续使用 `MockCommerceAdapter`。用一个 REST Adapter 测试实例断言本地 `update_price` Tool 经既有 Runtime 安全链最终调用注入实例，而不是绕过 PreToolUse 直接发 HTTP。注入的 Adapter 若不声明幂等或回查能力，写 Tool 在组装期拒绝启用。

- [x] **Step 2: 实现依赖注入且保持默认测试语义**

给 `build_product_application` 增加可选 `commerce_adapter: CommerceAdapter | None = None`；空值时创建并拥有 `MockCommerceAdapter` Session，显式注入时由调用方负责生命周期，`ProductApplication` 记录实际 Adapter。Eval 与 E2E 默认路径保持不变。

- [x] **Step 3: 更新架构文档和 README 的能力口径**

文档明确区分：Local Tool 为代码定义，MCP Tool 由 Source 同步装载，商品平台 REST 由 `CommerceAdapter` 对接；REST 当前用 Mock HTTP 契约验证，没有淘宝/抖音官方联调。README 不给出真实模型任务完成率，不把 Mock REST 结果描述成真实平台结果。

- [x] **Step 4: 跑发布级验证**

```powershell
uv run ruff check runtime agents adapters apps eval tests
uv run mypy runtime agents adapters apps eval
uv run pytest -q
npm --prefix apps/workbench run lint
npm --prefix apps/workbench run test -- --run
npm --prefix apps/workbench run build
npm --prefix apps/workbench run test:e2e
git diff --check
```

预期：所有命令退出 0；已记录实际通过数；Eval 确定性套件仍报告重复逻辑写为 0；真实模型评测和官方平台联调仍明确标为未运行。

- [x] **Step 5: 保留最终组合变更，不创建提交**

本轮不创建提交，改动保留在工作树供用户审阅。

预期：工作树保留未提交变更；最终报告区分 Mock REST 契约验证、确定性 Runtime Eval 和未执行的真实模型/官方平台评测。

## 实际执行结果

- REST Adapter 已通过组合根注入；新增组合测试验证写操作只有在 Runtime 确认后才发出，并沿用既有授权、确认与 Operation 状态链。
- Runtime 外部 I/O 事务边界、平台回查上下文、Eval 写入观察器及文档口径均已更新。
- 最终验证：`uv run pytest -q` 为 724 passed；Ruff、mypy、Workbench lint、49 项 Vitest、Workbench production build、2 项 Playwright E2E 与 `git diff --check` 均通过。
- 本轮没有执行真实模型评测、淘宝/抖音官方 API 联调或 Git commit；工作树保持未提交，供用户审阅。
