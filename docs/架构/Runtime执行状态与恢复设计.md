# Runtime 执行状态与恢复设计

> 版本：v0.3
> 日期：2026-10-19
> 状态：已确认（执行模型经 ADR-003 修订：单 API 进程、单 Runtime Executor）

## 1. 目标与边界

本文定义 Agent Runtime 的逻辑执行单元、分层状态机、人工交互、写操作确认、并发控制、幂等与回查、事件交付及配置版本语义。本文是数据库建模、Run 执行器、Runtime Eval 和故障注入测试的直接依据。

首版执行模型见 [ADR-003](ADR-003-首版执行模型.md)：单 API 进程、单 Runtime Executor，不使用 Job / 租约 / 心跳 / fencing token。本文中凡涉及"谁有权推进 Run"的表述，均以 ADR-003 为准。

本文不定义商品价格规则、淘宝或抖音协议细节，也不定义前端视觉样式。业务 Agent 负责领域输入要求与 Policy，Runtime 只执行公开接口并维护通用状态。

## 2. 核心不变量

1. 一条普通用户消息最多创建一个新的逻辑 Run。
2. Input Request、Confirmation Request、进程重启和失败重试只恢复原 Run。
3. 同一 Session 同一时刻最多存在一个非终态 Run；等待输入、等待确认和等待回查均算活动 Run。
4. `CANCELLED` 是终态，不能恢复；系统故障恢复不等于用户取消后恢复。
5. ToolCall 只有通过 Schema、业务输入、Access Control、Business Policy 和 Execution Mode 后才能执行。
6. Input Request 或 Confirmation Request 恢复后必须重新运行完整 PreToolUse，不能直接调用 Tool。
7. 同一逻辑写操作始终复用稳定 `operation_id` 和 idempotency key。
8. 外部写入结果不确定时不得盲目重试。
9. Runtime 状态与对应 AgentEvent 在同一数据库事务内提交。
10. 同一 Run 同一时刻只允许一个推进者；`state_version` CAS 是唯一的并发裁决，落败方必须丢弃本地结果。

## 3. 实体语义

### 3.1 Session

Session 是持续多轮对话的容器，保存消息、上下文摘要和当前活动 Run。一个 Session 可以依次包含多个 Run，但不能同时推进两个活动 Run。

### 3.2 Run

Run 是一次逻辑任务执行。普通用户消息创建新 Run；同一个 Run 可以经历多次执行器推进、人工等待、进程退出和恢复。

模型用自然语言询问后，本次 Run 正常结束；用户后续回复作为新的普通消息创建新 Run。只有结构化 Input Request 和 Confirmation Request 才挂起并恢复同一个 Run。

### 3.3 RunAttempt

RunAttempt 表示执行器对一个 Run 的一次实际推进。每次 `RunExecutor` 以 CAS 把 Run 从 `QUEUED` 推入 `RUNNING` 时创建一个新 Attempt，并递增 `attempt_no`。Attempt 用于区分重试和进程重启，不改变 Run 的逻辑身份，也不得生成新的 `operation_id`。`executor_id` 只用于事后归因，不参与互斥。

RunAttempt 状态：

| 状态 | 含义 |
|---|---|
| `RUNNING` | 执行器已 CAS 领取并开始推进 |
| `SUCCEEDED` | 本次推进到达新的持久边界或终态 |
| `FAILED` | 本次推进以确定性错误结束（失败码取自异常类型或结果） |
| `ABANDONED` | 进程在推进中退出；由下一次启动的恢复扫描收尾（`failure_code=EXECUTOR_RESTART`） |

### 3.4 ToolCall 与 Operation

ToolCall 是模型提出的一次工具调用。写 Tool 在所有必需参数完成规范化、且首次进入 Confirmation 或执行边界前，先在 ToolCall 上分配稳定 `operation_id`；恢复、确认和重试不得重新生成。分配 ID 不代表已经写入外部系统；真正执行前才以该 ID 创建 Operation。Operation 表示对外部系统的一次逻辑写入，不等同于网络请求次数。

## 4. 分层状态机

### 4.1 Run 状态

| 状态 | 含义 |
|---|---|
| `QUEUED` | 等待执行器领取推进（数据库即队列） |
| `RUNNING` | 执行器正在推进 Agent Loop 或 ToolCall |
| `WAITING_INPUT` | 等待 Input Request 回答 |
| `WAITING_CONFIRMATION` | 等待写操作确认或拒绝 |
| `WAITING_RECONCILIATION` | 存在不确定的外部写，等待回查 |
| `COMPLETED` | Run 正常结束 |
| `FAILED` | Run 以确定错误或无法自动解决的不确定结果结束 |
| `CANCELLED` | 用户取消，终态且不可恢复 |

合法迁移：

| 当前状态 | 允许迁移到 | 触发条件 |
|---|---|---|
| `QUEUED` | `RUNNING` | 执行器 CAS 领取成功 |
| `QUEUED` | `CANCELLED` | 用户在执行前取消 |
| `RUNNING` | `WAITING_INPUT` | 创建 Input Request |
| `RUNNING` | `WAITING_CONFIRMATION` | 创建 Confirmation Request |
| `RUNNING` | `WAITING_RECONCILIATION` | ToolCall 进入 `UNKNOWN` |
| `RUNNING` | `COMPLETED` | Agent 返回最终结果 |
| `RUNNING` | `FAILED` | 确定性不可恢复错误 |
| `RUNNING` | `CANCELLED` | 取消信号生效且当前无不可中断提交 |
| `WAITING_INPUT` | `QUEUED` | 合法回答写入，Run 重新入队 |
| `WAITING_INPUT` | `FAILED` | 请求过期 |
| `WAITING_INPUT` | `CANCELLED` | 用户取消 Run |
| `WAITING_CONFIRMATION` | `QUEUED` | 接受或拒绝结果写入，Run 重新入队 |
| `WAITING_CONFIRMATION` | `FAILED` | 请求过期 |
| `WAITING_CONFIRMATION` | `CANCELLED` | 用户取消 Run |
| `WAITING_RECONCILIATION` | `QUEUED` | 回查得到 `APPLIED` 或确定 `NOT_APPLIED` 且允许继续；执行器随后推进同一 Run |
| `WAITING_RECONCILIATION` | `FAILED` | 回查预算耗尽或 Adapter 无法消除不确定性 |

终态 `COMPLETED`、`FAILED`、`CANCELLED` 不允许迁移。非法迁移返回冲突错误并记录审计事件。

### 4.2 ToolCall 状态

| 状态 | 含义 |
|---|---|
| `PROPOSED` | 已从模型响应标准化 |
| `VALIDATING` | 正在执行 PreToolUse |
| `WAITING_INPUT` | 等待补充业务字段 |
| `WAITING_CONFIRMATION` | 等待写前确认 |
| `EXECUTING` | 已开始调用 Tool |
| `SUCCEEDED` | 已取得确定成功结果并完成必要回查 |
| `DENIED` | 被 Access Control、Business Policy 或 Execution Mode 拒绝 |
| `FAILED` | 取得确定失败结果 |
| `UNKNOWN` | 外部写入是否生效无法确认 |
| `CANCELLED` | ToolCall 未执行或在安全取消点终止 |

关键迁移：

- `PROPOSED → VALIDATING`；
- `VALIDATING → WAITING_INPUT / WAITING_CONFIRMATION / EXECUTING / DENIED / FAILED`；
- `WAITING_INPUT → VALIDATING`，回答只补入获准字段；
- `WAITING_CONFIRMATION → VALIDATING`，接受和拒绝均先恢复 Runtime；拒绝最终进入 `CANCELLED` 并作为 ToolResult 回填；
- `EXECUTING → SUCCEEDED / FAILED / UNKNOWN`；
- `UNKNOWN → SUCCEEDED / FAILED / VALIDATING`；只有回查确定 `NOT_APPLIED` 且 Adapter 策略允许时才能回到 `VALIDATING`，并在重新通过完整 PreToolUse 后执行。

### 4.3 执行器调度指令 `RunNextStep`

首版没有 `RunJob` 表。执行器与驱动方之间只用两条指令通信：

| 指令 | 含义 |
|---|---|
| `ADVANCE` | 本次推进到达可继续的边界，Run 退回 `QUEUED` 交给下一轮推进（如 `NOT_APPLIED` 重试） |
| `RECONCILE` | 存在不确定的外部写，必须先回查再决定是否继续 |

轮询不需要单独的指令：`QUEUED` Run 就是待办队列，执行器的兜底循环按 `created_at` 顺序扫描并 CAS 领取。

失败预算由 `RunAttempt` 承担：同一 Run 连续 `max_attempts` 次推进失败后落 `FAILED`，不再自动重试。`RunAttempt` 行数因此同时是审计记录与预算计数依据。

### 4.4 Request 状态

Input Request：`PENDING / ANSWERED / EXPIRED / CANCELLED`。

Confirmation Request：`PENDING / ACCEPTED / REJECTED / EXPIRED / CANCELLED`。

两类 Request 不共用 `ACCEPTED/REJECTED` 语义。同一请求只能从 `PENDING` 处理一次；重复提交返回幂等结果，不产生第二次推进。

## 5. Input Request

Input Request 至少保存：

- `request_id`、`workspace_id`、`session_id`、`run_id`、`tool_call_id`；
- 需要补充的字段路径；
- 问题文本、回答 JSON Schema 和服务端生成的候选项；
- `created_by_actor_id`、`answered_by_actor_id`；
- `status`、`expires_at`、`created_at`、`answered_at`；
- 创建时的 Tool Schema 版本和业务输入规则版本。

业务 Agent 的 InputRequirementResolver 负责把 Schema 问题或业务歧义转换成 InputRequirement。Runtime 负责持久化和恢复，不理解 `store_id`、平台或 SKU。

回答只能写入 Request 声明的字段，不能由前端替换整个 ToolCall 参数。恢复后从 Tool Resolution 开始重新执行完整 PreToolUse。

## 6. Confirmation Request 与 TOCTOU 防护

Confirmation Request 至少绑定：

- `actor_id`；
- `workspace_id / run_id / tool_call_id / operation_id`；
- Tool 名称；
- 规范化参数摘要；
- 资源 ID 与资源版本；
- Tool Schema、Business Policy 和 Execution Mode 版本；
- `expires_at` 与单次处理状态。

确认指纹使用规范化 JSON 计算：

```text
confirmation_fingerprint = SHA-256(
  tool_name
  + normalized_arguments
  + workspace_id
  + resource_ids_and_versions
  + operation_id
  + tool_schema_version
  + policy_version
)
```

确认后的恢复规则：

1. 重新校验当前 Actor 的成员身份、店铺范围和动作权限；
2. 重新读取业务资源并执行当前 Business Policy；
3. 重新计算确认指纹；
4. 只有指纹完全相同，旧确认才可以豁免再次询问；
5. 参数、资源版本、Policy 或作用域变化时，旧确认失效；如果新状态仍允许执行，创建新的 Confirmation Request；
6. `REJECTED` 不执行 Tool，将拒绝结果作为 ToolResult 回填给 Agent；
7. `AUTO_EXECUTE` 只跳过人工确认，不跳过授权、业务规则和资源版本检查。

## 7. 写操作与 Adapter 合同

写 Adapter 公开以下语义：

```text
execute(operation_id, normalized_input, idempotency_key) -> ExecutionReceipt
reconcile(operation_id, expected_state) -> APPLIED | NOT_APPLIED | UNKNOWN
supports_native_idempotency: bool
supports_reconciliation: bool
```

执行策略：

| Adapter 能力 | 超时或连接中断后的处理 |
|---|---|
| 原生幂等 | 使用相同 idempotency key 重试，并写后回查 |
| 无原生幂等但可可靠回查 | 先回查；仅在确定 `NOT_APPLIED` 后重试 |
| 既无幂等又无法可靠回查 | ToolCall 保持 `UNKNOWN`，禁止自动重试，Run 最终以明确错误结束 |

Runtime 不宣称任意外部系统上的 exactly-once。对支持幂等或可靠回查的 Adapter，目标是避免重复逻辑写入；对无法消除的不确定性，优先停止而不是猜测成功或重复执行。

## 8. 单执行器与并发控制

首版只有一个 Run 执行器，与 API 同进程（ADR-003）。没有 Job 表、租约、心跳和 fencing token，并发控制只剩两层：

1. **`Run.state_version` CAS（每次推进）**。所有状态迁移都是
   `UPDATE runs SET status = ?, state_version = state_version + 1
   WHERE id = ? AND workspace_id = ? AND state_version = ? [AND status = ?]`。
   零行更新表示有人先推进，本次结果必须丢弃。它保护的是进程内并发路径
   （路由唤醒与兜底扫描同时命中、连续两次确认、恢复扫描与请求内推进重叠），
   不是为多实例准备的。
2. **进程级单写者锁（每次启动）**。`SingleWriterLock` 以 PostgreSQL advisory lock
   断言同库只有一个执行器；获取失败直接拒绝启动，不降级为只读。它只在启动时
   竞争一次，不参与任务调度，也不需要续期。

推进过程中不持有数据库长事务：AgentLoop 在模型调用与 Tool 调用前提交 checkpoint，
`OperationService.begin` / `record_result` 各是一个短事务。因此"执行器是否仍持有
执行资格"不再是一个需要续期的状态，需要保证的只是每个提交都带上正确的
`state_version` 与"Run 仍可执行"的前置条件。

已经发出的外部请求无法被撤回，因此写入正确性仍依赖稳定 `operation_id`、平台
幂等键与回查（§7），与执行器数量无关。

## 9. AgentEvent 交付

每个 AgentEvent 至少包含：

- 全局唯一 `event_id`；
- `workspace_id / session_id / run_id`；
- Run 内单调递增的 `sequence_no`；
- `event_type`、Schema 版本、发生时间和脱敏后的 payload；
- 关联的 `attempt_id`、`tool_call_id` 或 Request ID。

状态变更与 AgentEvent 在同一 PostgreSQL 事务内提交。API/WebSocket 采用 at-least-once 交付，不宣称 exactly-once；客户端按 `event_id` 去重，使用 `(run_id, sequence_no)` 检测缺口，并可携带 cursor 从 PostgreSQL 重放。

工作台 `EventReducer` 维护 `seen_event_ids`、每个 Run 的 `last_sequence_no`、当前投影与 `needs_snapshot`。重复事件直接忽略；序号小于等于已应用序号的迟到事件不回滚视图；序号出现缺口时停止应用后续事件并重新获取包含 cursor 的快照；终态一旦由连续事件或新快照确认，旧事件不能恢复为活动态。Reducer 只产生 UI 投影，不向 Runtime 回写执行状态。

模型密钥、认证 Header、完整凭证和业务敏感字段不得写入 AgentEvent、日志或 Langfuse Trace。

## 10. 配置版本与恢复

Run 创建时固定并记录：

- `agent_version`、Prompt 版本；
- 使用的 SkillVersion 集合；
- Tool Schema 版本与 Tool 实现版本；
- AgentBackend 类型、模型配置版本和非敏感参数摘要；
- Source/MCP 配置版本；
- 创建时的业务规则版本。

模型推理、上下文重建和 Tool 参数解释使用上述快照，恢复时不静默切换版本。凭证只保存 Secret 引用，不复制明文。

身份权限、店铺范围、Execution Mode、Business Policy 和资源状态属于执行时安全事实，每次写入前按当前版本重新读取。新版本使旧确认指纹失效时必须重新确认或拒绝执行。

## 11. AgentBackend 能力边界

`AgentBackend` 必须声明 `AgentBackendCapabilities`，至少包含：

- `tool_calling`；
- `streaming`；
- `structured_output`；
- `cancellation`；
- `usage_reporting`；
- `parallel_tool_calls`。

业务 Agent 在注册时声明必需能力，Runtime 在创建 Run 前完成能力匹配。缺少必需能力时 fail-fast，不能通过修改 Prompt 或静默降级伪装支持。

首版要求生产 Backend 支持 Tool Calling 和 Streaming。Structured Output、Cancellation 和 Usage Reporting 按能力显式分支；Runtime 不依赖它们作为安全边界。首版禁用并行 Tool 执行；即使 Backend 能返回多个 ToolCall，Runtime 也按稳定顺序逐个校验和执行。

Provider 可移植性只承诺共同能力集合内的行为一致，不承诺不同模型产生相同决策或文本结果。

## 12. 外部依赖降级

- Langfuse 不可用：Runtime 执行继续，Trace 异步补偿或记录同步失败；
- Mem0 不可用：跳过长期记忆检索与写回，不影响权威 Session/Run 状态；
- WeKnora 不可用：标记为可选知识的任务可以降级；Tool 或 Skill 声明知识为决策必要依据时必须 fail closed；
- MCP Tool 不可用：产生确定的 ToolResult 错误，由 Agent 或 Runtime 错误策略处理；
- 任何外部依赖都不是身份、授权、确认或 Runtime 状态的权威来源。

## 13. 取消、超时与恢复

- 用户取消将 Run 置为 `CANCELLED`，取消仍在等待的 Request 和未执行的 ToolCall；
- 对已发出的外部写请求，取消不能伪装成“未执行”，必须先进入回查语义；
- 进程退出后由启动恢复扫描接管同一个 Run，新 Attempt 记录推进，遗留 Attempt 标为 `ABANDONED`；
- Input/Confirmation 过期导致 Run `FAILED`，错误码分别为 `INPUT_EXPIRED` 或 `CONFIRMATION_EXPIRED`；
- 存在 `EXECUTING` / `UNKNOWN` Operation 时由执行器先回查再推进；达到回查预算仍不确定时，Run `FAILED`，但 ToolCall 保留 `UNKNOWN` 作为权威事实。

## 14. 验收用例

以下确定性用例必须全部通过：

1. 普通用户消息创建新 Run，结构化交互恢复原 Run；
2. 进程重启产生新 RunAttempt，但不改变 Run、ToolCall 和 operation ID；
3. 同一 Session 存在等待中 Run 时拒绝创建普通 Run；
4. Input 回答只能修改获准字段，恢复后重新执行完整 PreToolUse；
5. Confirmation 参数、资源版本或 Policy 变化后旧确认失效；
6. 权限在确认后被撤销时，写操作被拒绝；
7. 同一 Run 的第二个推进者被 `state_version` CAS 拒绝，无法提交状态；
8. Tool 已写入但响应丢失时先回查，不直接重试；
9. 不可幂等且不可回查的 Adapter 在 `UNKNOWN` 后不重试；
10. Event 重复投递时客户端可按 event ID 去重，断线后可按 cursor 重放；
11. `CANCELLED` Run 不能恢复；
12. Mem0、WeKnora 或 Langfuse 故障不改变 Runtime 权威状态和授权结果。
13. Event 乱序或缺口不会产生错误 UI 终态，缺口触发快照恢复；
14. Memory 被纠正、过期或删除后，即使 Mem0 返回旧记录也不会进入 Context。

危险副作用与重复逻辑写入必须报告为 `0/N`，其中 `N` 是实际执行的对抗或恢复用例数；基础设施失败不得混入通过率。

## 15. 实现验证状态

以下崩溃恢复目标已由 `tests/runtime/integration/test_crash_recovery.py`（计划 02 Task 10，crash_harness 故障注入 + 真实 Runtime 服务与真库）按 ADR-003 的执行模型验证；本节仅为状态标记，不改变上述设计语义。

| 已验证目标（对应本文小节/验收用例） | 验证测试 |
|---|---|
| 执行前进程崩溃：Run 停在 `RUNNING`，重启扫描收尾 `ABANDONED` 并推进到 `COMPLETED`（§6、§13、验收 2） | `test_interrupted_run_recovers_and_completes` |
| `EXECUTING` Operation 在进程退出后先回查，再恢复推进（§6、§13） | `test_executing_operation_exit_reconciles_before_resuming` |
| 外部写入生效但响应丢失：先回查 `APPLIED`，不直接重试、无二次写（§7、§13、验收 8） | `test_response_loss_reconciles_without_second_write` |
| 回查 `UNKNOWN → APPLIED`：预算内继续回查，消除不确定性后推进同一 Run（§4.1、§7） | `test_reconcile_unknown_then_applied_resumes_run` |
| 同一 Run 的第二个推进者被 `state_version` CAS 拒绝（§8、验收 7） | `test_second_advance_is_blocked_by_state_version_cas` |
| ToolResult Schema 失败不清除已发生写入：`APPLIED` 保持、`INVALID_TOOL_RESULT`、`retry_allowed=False`、权威事实不改写（§7） | `test_schema_failure_keeps_applied_write_and_authoritative_fact` |
| Context 压缩不丢 Protected Context：安全规则、当前用户消息、待确认 fingerprint 存活（Task 5 Protected 语义） | `test_compression_preserves_protected_context` |
| 单写者锁拒绝第二个执行器（ADR-003 §5.2） | `test_single_writer.py::test_second_executor_is_rejected_while_lock_is_held` |

恢复类评测用例（`eval/recovery_suite.py`）：`executor-exit-before-write`、`executor-restart-after-confirmation` 覆盖"先回查再推进"的顺序；`eval/runtime_suite.py` 的 `write-result-rejected-after-cancel` 覆盖"取消后迟到的写结果不得落库"。
