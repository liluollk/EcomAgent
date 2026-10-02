# PostgreSQL 数据模型与事务设计

> 版本：v0.2
> 日期：2026-10-02
> 状态：已确认

## 1. 目标与选型

本文把需求和 Runtime 状态机映射为 PostgreSQL 表、约束、索引和事务边界，是 SQLAlchemy Model、Repository、Alembic Migration 和持久化测试的依据。

首版使用 PostgreSQL 作为 Runtime、身份、业务配置和审计的权威存储，不使用 SQLite 兼容模式，也不引入 Redis。原因是实现依赖 JSONB、部分唯一索引、`FOR UPDATE SKIP LOCKED`、事务锁和 pgvector；使用 SQLite 测试无法验证这些关键语义。

默认事务隔离级别使用 `READ COMMITTED`，在需要串行化的 Session、Request 和 Job 行上使用条件更新或行锁，不把整个 Agent Loop 放入数据库事务。

## 2. 数据库与 Schema 边界

主应用数据库为 `agent_runtime`，包含：

- 身份与 Workspace；
- Agent、Skill、Source、Tool 和模型配置；
- Session、Run、Tool 执行和交互状态；
- AgentEvent 与审计；
- 长期记忆权威记录和外部集成 Outbox；
- 商品运营 Mock 业务状态。

Mem0 可以使用同一 PostgreSQL 实例中的独立数据库或 Schema 保存其内部索引，但这些表不是业务权威源。`agent_runtime.memory_records` 保存可审计的权威内容和同步状态，跨库同步通过 Outbox 完成，不使用跨数据库事务。

所有时间以 UTC `timestamptz` 保存；主键默认使用 UUID。面向用户的排序使用服务端生成时间和稳定主键，不依赖客户端时间。

## 3. Workspace 与身份表

### 3.1 核心表

```text
users
  id, login, password_hash, status, created_at, updated_at

workspaces
  id, name, status, execution_mode, state_version, created_at, updated_at

workspace_memberships
  id, workspace_id, user_id, role, status, created_at, updated_at

membership_resource_scope_modes
  workspace_id, membership_id, resource_type, mode

membership_resource_scopes
  workspace_id, membership_id, resource_type, resource_id

membership_permissions
  workspace_id, membership_id, permission
```

关键约束：

- `users.login` 唯一；
- `(workspace_id, user_id)` Membership 唯一；
- 每个 `resource_type` 的 mode 只允许 `ALL / SELECTED`；`SELECTED` 使用 membership_resource_scopes，空集合表示该类资源无权限；
- `(workspace_id, membership_id, resource_type)` Scope Mode 唯一；
- `(workspace_id, membership_id, resource_type, resource_id)` 唯一；Runtime 不对 `resource_type` 编码商品领域分支；
- Membership、Scope 和 Permission 使用包含 `workspace_id` 的复合外键，禁止跨 Workspace 引用；多态 `resource_id` 由所属业务 Agent 在配置时和每次授权时验证，删除业务资源时必须清理对应 Scope；
- 每个有效 Workspace 至少保留一个 WorkspaceOwner；删除最后一个 Owner 的事务必须被应用服务拒绝。

角色使用受控 Enum `WORKSPACE_OWNER / OPERATOR`，不在首版建设可编辑角色系统。动作权限以字符串 Enum 存储并由代码版本管理。

## 4. 版本化配置表

### 4.1 Agent、Skill、Tool 与 Source

```text
agents
  id, stable_key, name, created_at

agent_versions
  id, agent_id, version, status, definition_json, definition_hash, published_at

skills
  id, scope, workspace_id nullable, stable_key, name

skill_versions
  id, workspace_id nullable, skill_id, version, status,
  content_json, content_hash, published_at

tool_definitions
  id, scope, workspace_id nullable, stable_key, name

tool_versions
  id, workspace_id nullable, tool_id, version, effect,
  input_schema, output_schema, schema_hash,
  implementation_key nullable, source_version_id nullable,
  status, published_at

sources
  id, workspace_id, stable_key, kind, name

source_versions
  id, workspace_id, source_id, version, config_json, config_hash,
  secret_id, status, published_at
```

发布版本不可原地更新；新内容必须创建新版本。`definition_hash/content_hash/schema_hash/config_hash` 用于启动校验和恢复审计。

Agent 是代码随附的全局目录。Skill 允许 `GLOBAL / WORKSPACE` 两种作用域：内置 Skill 全局可用，Workspace Skill 只保存数据化内容，不执行动态代码。Local Tool 是全局定义；MCP Tool 是 Workspace 资源，必须关联产生它的 SourceVersion。Source、模型连接、Business Policy 及其版本属于 Workspace 资源。

`scope = GLOBAL` 时 `workspace_id` 必须为空，`scope = WORKSPACE` 时必须非空；使用 Check Constraint 强制该不变式。MCP Tool 的 `workspace_id` 必须与 SourceVersion 一致，并使用复合外键防止跨 Workspace 引用。所有 Workspace 版本表都必须保存 `workspace_id`。

Workspace 绑定表：

```text
workspace_agent_bindings
workspace_skill_bindings
workspace_tool_bindings
workspace_source_bindings
```

绑定表保存 `workspace_id`、对应 Version ID、启用状态和生效时间。

### 4.2 模型与 Policy

```text
model_connections
  id, workspace_id, stable_key, name

model_config_versions
  id, workspace_id, model_connection_id, version, backend_type, base_url, model,
  non_secret_options, secret_id, config_hash, status, published_at

business_policies
  id, workspace_id, stable_key, name

business_policy_versions
  id, workspace_id, business_policy_id, version,
  config_json, config_hash, status, published_at
```

`backend_type` 只允许 `OPENAI / ANTHROPIC`。Base URL 保存规范化结果并经过 allowlist 校验。Secret 仅通过 `secret_id` 引用。

### 4.3 参考 Agent 业务数据

商品运营参考 Agent 的 Store、Product、Listing 和 Price Lock 位于独立 `product_operations` Schema，不属于 Runtime 核心表。Runtime 通过通用 `membership_resource_scopes` 保存 `resource_type = store` 的资源引用，不导入商品 Model。具体表结构、唯一约束和 Mock 权威状态以 `docs/架构/商品运营参考Agent设计.md` 为准。

## 5. Session、Message 与 Run

```text
sessions
  id, workspace_id, agent_id, created_by_actor_id, created_by_membership_id,
  title, status, active_run_id, created_at, updated_at

messages
  id, workspace_id, session_id, run_id nullable,
  role, status, content, structured_content,
  sequence_no, covered_from_sequence nullable, covered_to_sequence nullable,
  summary_schema_version nullable, source_hash nullable,
  created_at, completed_at

runs
  id, workspace_id, session_id, created_by_actor_id, created_by_membership_id,
  trigger_message_id, status, state_version, next_event_sequence,
  agent_version_id, model_config_version_id, prompt_version,
  deadline_at, failure_code, created_at, updated_at, completed_at

run_attempts
  id, workspace_id, run_id, attempt_no, worker_id,
  status, started_at, finished_at, failure_code
```

Message 约束：

- `(session_id, sequence_no)` 唯一；
- Assistant Message 状态只允许 `STREAMING / COMPLETED / ABORTED`；
- Context 查询只读取允许的 Role 且状态为 `COMPLETED` 的 Message。
- `SYSTEM_SUMMARY` Message 必须同时保存覆盖区间、Schema 版本和来源 Hash，且只能覆盖同一 Session 内已完成的连续消息；

Run 约束：

- `(run_id, attempt_no)` RunAttempt 唯一；
- 使用部分唯一索引保证同一 Session 最多一个活动 Run：

```sql
UNIQUE (session_id)
WHERE status IN (
  'QUEUED', 'RUNNING', 'WAITING_INPUT',
  'WAITING_CONFIRMATION', 'WAITING_RECONCILIATION'
)
```

- `sessions.active_run_id` 是查询优化字段，必须与活动 Run 在同一事务内更新；数据库部分唯一索引才是最终防线；
- Run 的终态不可通过普通 Repository API 更新回活动状态。

首版 Session 只允许 `created_by_actor_id` 对话读写和 Request 交互。WorkspaceOwner 的审计查询走独立脱敏视图，不通过 Session Repository 绕过可见性。

## 6. Run 配置快照

Run 使用关联表固定实际版本：

```text
run_skill_versions(workspace_id, run_id, skill_version_id)
run_tool_versions(workspace_id, run_id, tool_version_id)
run_source_versions(workspace_id, run_id, source_version_id)
run_policy_snapshots(workspace_id, run_id, policy_key, proposed_version_id, config_hash)
```

Run 保存版本 ID 和非敏感摘要，不复制 API Key。模型配置通过不可变版本 ID 引用 Secret。

权限、Execution Mode、Business Policy 和业务资源状态在写操作前按当前数据重新读取；Run Policy Snapshot 用于解释模型当时看到的规则，不覆盖当前安全事实。

## 7. ToolCall、Operation 与执行尝试

```text
tool_calls
  id, workspace_id, run_id, message_id,
  tool_version_id, ordinal, status,
  raw_arguments, normalized_arguments,
  operation_id nullable, failure_code,
  created_at, updated_at

operations
  id, workspace_id, tool_call_id, operation_id,
  idempotency_key, adapter_key,
  supports_native_idempotency, supports_reconciliation,
  expected_state, status, last_receipt,
  created_at, updated_at

operation_attempts
  id, workspace_id, operation_row_id, attempt_no, kind,
  request_fingerprint, status, external_reference,
  started_at, finished_at, failure_code
```

关键约束：

- `(run_id, ordinal)` ToolCall 唯一，保留模型返回顺序；
- 写 ToolCall 在所有必需参数完成规范化后必须拥有稳定 `operation_id`；只读 ToolCall 的 `operation_id` 为空；
- Operation 行在真正进入外部写执行边界时创建；等待 Confirmation 的 ToolCall 可以只有 `operation_id` 而尚无 Operation 行；
- `operations.operation_id`、`operations.tool_call_id` 和 `idempotency_key` 分别唯一；
- `(operation_row_id, attempt_no)` OperationAttempt 唯一；`operation_row_id` 外键指向 `operations.id`；
- `kind` 为 `EXECUTE / RECONCILE`；
- 原始参数只用于诊断并按字段脱敏，执行始终使用规范化参数；
- 外部响应体只保存允许字段，不保存凭证、Cookie 或认证 Header。

Operation 状态反映逻辑写入：`PREPARED / EXECUTING / APPLIED / NOT_APPLIED / UNKNOWN / FAILED`。

## 8. Input 与 Confirmation

```text
input_requests
  id, workspace_id, session_id, run_id, tool_call_id,
  field_paths, question, response_schema, options_json,
  status, created_by_actor_id, answered_by_actor_id,
  expires_at, created_at, answered_at

confirmation_requests
  id, workspace_id, session_id, run_id, tool_call_id, operation_id,
  actor_id, fingerprint, normalized_arguments_hash,
  resource_versions, tool_schema_version, policy_version,
  execution_mode_version, status, expires_at,
  created_at, handled_at
```

部分唯一索引保证每个 ToolCall 同时最多存在一个 `PENDING` Input Request 和一个 `PENDING` Confirmation Request。

一个 Input Request 可以一次要求多个相关字段，`field_paths`、`response_schema` 和候选项必须相互一致；前端回答只能填充这些路径。

Request 处理使用条件更新：

```sql
UPDATE ...
SET status = :resolved_status, ...
WHERE id = :id
  AND workspace_id = :workspace_id
  AND status = 'PENDING'
  AND expires_at > now()
RETURNING *;
```

返回零行表示已处理、已过期、越权或不存在；应用服务必须进一步区分并返回稳定错误码。

## 9. RunJob 与租约

```text
run_jobs
  id, workspace_id, run_id, kind,
  status, available_at, attempt_count, max_attempts,
  lease_owner, lease_token, lease_expires_at,
  last_error_code, created_at, updated_at
```

`kind` 只允许：

- `ADVANCE_RUN`：推进 Agent Loop、处理 ToolCall 或交互恢复；
- `RECONCILE_OPERATION`：回查 `UNKNOWN` Operation。

Trace 和 Memory 同步不使用 RunJob，进入 Integration Outbox。

部分唯一索引保证同一 Run 最多一个活动 Job：

```sql
UNIQUE (run_id)
WHERE status IN ('PENDING', 'LEASED')
```

领取过程在短事务中执行：

1. 使用 `FOR UPDATE SKIP LOCKED` 选择到期的 `PENDING` Job；
2. 写入新 `lease_token`、Worker ID 和过期时间；
3. 将 Job 改为 `LEASED`；
4. 创建新 RunAttempt 并递增 `attempt_no`；
5. `ADVANCE_RUN` 将 Run 从 `QUEUED` CAS 为 `RUNNING`；`RECONCILE_OPERATION` 要求 Run 已是 `WAITING_RECONCILIATION`，领取期间保持该状态；
6. 提交事务后才开始模型或 Tool 外部调用。

租约回收器处理过期 Job：

- 锁定 Job 和 Run；
- 把旧 Attempt 标记 `ABANDONED`；
- 清除旧 token 并生成新的可领取周期；
- 未超过预算时 Job 回到 `PENDING`；`ADVANCE_RUN` 的 Run 回到 `QUEUED`，`RECONCILE_OPERATION` 的 Run 保持 `WAITING_RECONCILIATION`；
- 超过预算时 Job 进入 `DEAD`、Run 进入 `FAILED`；
- 写 Operation 已处于 `EXECUTING/UNKNOWN` 时，下一 Job 必须为 `RECONCILE_OPERATION`，不能直接再次执行。

## 10. AgentEvent 与审计

```text
agent_events
  id, workspace_id, session_id, run_id,
  sequence_no, event_type, schema_version,
  payload, attempt_id, tool_call_id,
  created_at

audit_events
  id, workspace_id, actor_id, action,
  target_type, target_id, outcome, reason_code,
  request_id, run_id, tool_call_id,
  redacted_payload, created_at
```

约束与索引：

- `agent_events.id` 是 event ID；
- `(run_id, sequence_no)` 唯一；
- `(workspace_id, run_id, sequence_no)` 用于重放；
- Audit Event 只追加，不提供普通 Update/Delete Repository；
- payload 进入数据库前完成字段级脱敏。

Run 行保存 `next_event_sequence`。写状态与事件时锁定或 CAS Run，原子取得并递增序号；不使用 `max(sequence_no) + 1`。

AgentEvent 表同时承担持久事件与 WebSocket Outbox。API 只发布已提交事件；重复发布由客户端按 event ID 去重。

## 11. Secret 与外部集成 Outbox

```text
secrets
  id, workspace_id, kind, endpoint_binding,
  key_version, nonce, ciphertext, auth_tag,
  created_by_actor_id, created_at, rotated_at

memory_records
  id, workspace_id, actor_id, kind, subject_key,
  content, content_hash, source_type, source_id, source_session_id,
  revision, supersedes_id, lifecycle_status, sync_status,
  valid_from, expires_at,
  deleted_at, deleted_by_actor_id, created_at, updated_at

integration_outbox
  id, workspace_id, kind, aggregate_type, aggregate_id,
  payload_ref, status, available_at, attempt_count,
  lease_owner, lease_token, lease_expires_at,
  last_error_code, created_at, updated_at
```

Outbox `kind` 首版包括 `MEMORY_UPSERT`、`MEMORY_DELETE` 和 `TRACE_EXPORT`。业务事务只写权威状态和 Outbox 行，不在同一事务中调用 Mem0 或 Langfuse。

Memory Record 生命周期状态为 `ACTIVE / SUPERSEDED / EXPIRED / DELETED`，同步状态为 `PENDING / SYNCED / FAILED`；Outbox 状态为 `PENDING / LEASED / DONE / DEAD`。本地生命周期与外部同步状态分开，避免 Mem0 暂时失败导致一条本地有效记忆被错误视为失效。

同一 `(workspace_id, actor_id, kind, subject_key)` 只有最新且生命周期为 `ACTIVE`、尚未过期的 Revision 可以进入 Context。用户纠正时在单个事务中创建新 Revision、设置 `supersedes_id`、把旧记录改为 `SUPERSEDED` 并写 `MEMORY_UPSERT`；用户删除时改为 `DELETED`、记录删除人和时间并写 `MEMORY_DELETE`。Runtime 在接收 Mem0 检索结果后仍按本地 Memory ID、Revision、生命周期和有效期过滤，因此外部索引延迟不能复活失效记忆。首版不根据向量相似度自动合并或裁决不同 `subject_key` 的语义冲突。

Outbox Worker 成功后标记 `DONE`；失败按预算重试，最终 `DEAD` 并保留诊断。外部集成失败不能回滚已经完成的 Runtime 状态。

Outbox 使用与 RunJob 相同的短租约原则，但其租约和失败不会改变 Run 状态。

## 12. 核心事务

### 12.1 创建 Run

一个事务内：

1. 校验 Actor、Session 和 AgentBackend 能力；
2. 锁定 Session；
3. 验证不存在活动 Run；
4. 插入 User Message、Run 和版本关联；
5. 更新 `sessions.active_run_id`；
6. 插入 `ADVANCE_RUN` Job；
7. 写 `RUN_CREATED` AgentEvent 和审计事件；
8. 提交。

### 12.2 推进到等待状态

一个事务内：

1. 验证 Job lease token 和 Run `state_version`；
2. 更新 ToolCall；
3. 创建 Input 或 Confirmation Request；
4. Run 改为对应等待状态；
5. 当前 Job 改为 `DONE`，Attempt 改为 `SUCCEEDED`；
6. 写 AgentEvent；
7. 提交。

### 12.3 回答 Request

一个事务内：

1. 锁定 Request、Run 和 Session；
2. 校验 Workspace、Actor、状态和过期时间；
3. 条件更新 Request；
4. Run 改为 `QUEUED`；
5. 创建唯一 `ADVANCE_RUN` Job；
6. 写 AgentEvent 和审计事件；
7. 提交。

### 12.4 外部写操作

阶段一事务：

1. 完成全部 PreToolUse；
2. 创建或读取稳定 Operation；
3. ToolCall 与 Operation 改为执行中；
4. 插入 OperationAttempt 和 `TOOL_EXECUTION_STARTED` 事件；
5. 提交。

事务外调用 Adapter。

阶段二事务：

1. 验证 lease token、Run state version 和 Operation 当前状态；
2. 保存脱敏 ExecutionReceipt；
3. 确定结果则更新 ToolCall/Operation；不确定则进入 `UNKNOWN` 与 `WAITING_RECONCILIATION`；
4. 写 AgentEvent；
5. 提交。

如果 Worker 在两个事务之间退出，租约回收后的 Job 根据 Operation 状态进入回查，不重新生成 Operation。

### 12.5 完成 Run

一个事务内：

1. 验证没有未完成 ToolCall、Request 或 Operation；
2. Run 改为终态；
3. Session 清除 `active_run_id`；
4. Job 和 Attempt 结束；
5. Assistant 最终 Message 与 AgentEvent 持久化；
6. 生成 Memory/Trace Outbox；
7. 提交。

### 12.6 回查 Operation

回查前在短事务内验证 `RECONCILE_OPERATION` Job 的 lease token、Run 状态和 Operation 当前状态，插入状态为 `RUNNING` 的 `RECONCILE` OperationAttempt，然后提交。回查网络调用在事务外执行。

保存结果时在一个事务内：

1. 再次验证 lease token、Run 状态和 Operation 当前状态；
2. 完成 OperationAttempt 并保存脱敏结果；
3. 结果为 `APPLIED`：Operation 与 ToolCall 成功，Run 改为 `QUEUED`；
4. 结果为 `NOT_APPLIED`：仅当 Adapter 策略允许安全重试时，Operation 回到 `PREPARED`、ToolCall 回到 `VALIDATING`、Run 改为 `QUEUED`；恢复后必须重新执行完整 PreToolUse，不得直接重试写入；否则 Run 失败；
5. 结果仍为 `UNKNOWN`：未超过预算则创建下一次延迟 `RECONCILE_OPERATION` Job，并保持 `WAITING_RECONCILIATION`；超过预算则 Run 失败且 ToolCall 保留 `UNKNOWN`；
6. 当前 Job 与 Attempt 结束；需要继续 Agent Loop 时创建唯一 `ADVANCE_RUN` Job；
7. 写 AgentEvent 并提交。

### 12.7 Request 过期

服务端过期任务按 `expires_at` 扫描 `PENDING` Request，并逐个在短事务中：

1. 锁定 Request、Run 和 Session；
2. 再次确认 Request 仍为 `PENDING` 且已经过期；
3. Request 改为 `EXPIRED`，对应 ToolCall 失败；
4. Run 改为 `FAILED`，Session 清除 `active_run_id`；
5. 取消关联的活动 Job；
6. 写 AgentEvent 和审计事件；
7. 提交。

Request 过期任务只能收敛状态，不能代替用户回答或自动确认。

## 13. Repository 与事务所有权

Repository 只执行带 Workspace 条件的持久化操作，不自行提交事务。Application Service 拥有事务边界并协调多个 Repository。

禁止：

- Repository 内调用模型、MCP、Mem0、Langfuse 或平台 API；
- ToolHandler 直接取得数据库 Session 并修改 Runtime 表；
- API Handler 手工拼接跨 Workspace 查询；
- 在外部网络调用期间持有行锁；
- 使用内存队列保存唯一一份待执行 Job。

允许只读查询使用独立短事务。所有写 Repository 方法必须接收 `workspace_id`，关键状态更新还必须接收 `expected_state_version` 或 lease token。

## 14. 索引与保留策略

首版至少建立：

- Session：`(workspace_id, updated_at desc)`；
- Run：`(workspace_id, session_id, created_at)`、活动状态部分索引；
- Job：`(status, available_at)`、租约过期索引；
- ToolCall：`(run_id, ordinal)`；
- Event：`(run_id, sequence_no)`；
- Audit：`(workspace_id, created_at desc)`、`(run_id, created_at)`；
- Request：`(run_id, status)` 和 `expires_at`；
- Operation：`operation_id`、`tool_call_id`；
- Outbox：`(status, available_at)`。

首版不实现自动删除 Runtime 状态和审计记录。测试数据提供显式清理命令；生产保留策略属于部署配置，不能通过级联删除 Workspace 静默抹除审计。

## 15. Alembic 与验证

Alembic Migration 按可回滚的小步提交：

1. 身份与 Workspace；
2. 版本化配置；
3. Session、Message、Run 与 Attempt；
4. ToolCall、Operation 与 Request；
5. Job、Event、Audit 与 Outbox；
6. pgvector 与 Memory 表。

每次 Migration 在空数据库和上一版本数据库上验证 upgrade；破坏性 downgrade 不作为首版承诺，但 Migration 必须显式说明不可逆原因。

持久化验收至少覆盖：

1. 部分唯一索引阻止同 Session 多活动 Run；
2. 部分唯一索引阻止同 Run 多活动 Job；
3. 复合外键阻止跨 Workspace 引用；
4. CAS 阻止旧 state version 提交；
5. fencing token 阻止失租 Worker 提交；
6. Request 并发回答只成功一次；
7. Event sequence 在并发提交时保持唯一递增；
8. Worker 退出后 Job 回收产生新 Attempt；
9. 外部写入前后崩溃均进入正确 Operation/回查状态；
10. Outbox 失败不回滚 Runtime 权威事务；
11. Secret 明文不出现在普通查询、日志和事件；
12. 所有持久化集成测试运行在真实 PostgreSQL，而不是 SQLite。
