# Runtime 核心接口与 Harness 设计

> 版本：v0.2
> 日期：2026-10-02
> 状态：已确认

## 1. 目标与设计取舍

本文定义 Runtime 的公开接口、Agent Harness、Context 装配、AgentBackend、Skill/Source/Tool 注册、PreToolUse 与 ToolResult 处理边界。目标是让商品运营 Agent 只依赖 Runtime 公开接口，同时避免为了“通用”提前建设低代码插件平台或通用工作流引擎。

采用“显式接口 + 启动时组装”的方案：

- 可持久化定义只保存数据和版本，不保存 Python 函数；
- 业务代码在进程启动时注册 Handler、输入要求解析器和 Policy；
- Runtime 校验数据库配置与代码注册是否匹配，缺失时 fail-fast；
- Runtime 核心不反向导入 `agents/`、`adapters/` 或 `apps/`。

不采用装饰器自动扫描全仓库、动态执行数据库脚本、前端编排任意节点和运行时下载插件。

## 2. 模块边界

```text
apps/api ───────────────┐
apps/worker ────────────┼──→ Runtime public interfaces
agents/product_operations ─┤
adapters/* ─────────────┘
                              ↓
runtime internal services / repositories / harness
```

依赖规则：

1. `runtime/interfaces/` 只包含 Pydantic 数据结构、Enum 和 Protocol，不导入 SQLAlchemy、FastAPI、具体 Provider SDK 或业务 Agent；
2. `runtime/` 不导入 `agents/`、`adapters/`、`apps/` 和 `eval/`；
3. `agents/` 依赖 Runtime 公共接口，定义 Agent、Skill、Tool 元数据和业务扩展；
4. `adapters/` 实现 Runtime Protocol 或业务 Agent 定义的领域端口；
5. `composition/` 是商品运营产品组合根，负责把 Agent 定义、Handler、Repository、Backend 和外部集成组装成 Runtime；`apps/worker/` 负责 Worker 生命周期与任务调度；
6. `apps/api/` 只通过应用服务提交命令和读取状态，不直接调用 AgentBackend 或 ToolHandler。

架构测试必须扫描 import，阻止 Runtime 对上层模块形成反向依赖。

Agent Runtime 包含两组职责：

- 运行配置与治理：Workspace、Actor、权限、版本、Secret 和运行配置；
- 执行内核：Session、Run、Job、工具调用、交互、恢复和事件。

这只是同一模块化单体中的逻辑职责划分，不对应独立进程、微服务或代码目录。Agent Harness 属于执行内核中面向模型的编排部分。

## 3. 公共定义与运行时实现分离

### 3.1 AgentDefinition

`AgentDefinition` 是可版本化数据，至少包含：

```text
agent_id
agent_version
name
system_prompt_version
required_backend_capabilities
required_skill_keys
required_tool_keys
required_source_keys
context_policy
loop_limits
```

发布后的 AgentVersion 不可原地修改；修改产生新版本。AgentVersion 只声明所需能力的稳定 Key，Workspace 绑定具体已发布版本，Run 创建时再将实际解析结果固定为精确 Version ID。这样全局内置 Agent 不会引用某个 Workspace 的 Source。

### 3.2 ToolDefinition

`ToolDefinition` 是模型可见且可持久化的数据：

```text
tool_name
tool_version
description
input_json_schema
output_json_schema
effect: READ | WRITE
timeout_seconds
required_permissions
source_kind: LOCAL | MCP
schema_hash
```

`ToolDefinition` 不包含回调、闭包、客户端对象、数据库 Session 或凭证。

Local Tool 是代码随附的全局定义；MCP Tool 由 Source 同步产生，必须带 `workspace_id` 和 `source_version_id`。两者在 BackendRequest 中采用同一 ToolDefinition 形式，但持久化作用域不同。

### 3.3 ToolBinding

`ToolBinding` 只存在于 Worker 进程，连接数据定义与可执行实现：

```text
definition
handler
input_normalizer
argument_provenance_resolver
input_requirement_resolver
authorization_resource_resolver
business_policy
result_normalizer
write_adapter_capabilities
```

只读 Tool 不需要写 Adapter 能力。写 Tool 必须提供稳定 Operation 语义和 Reconciliation 实现；无法回查时显式声明不支持。

### 3.4 SkillVersion

SkillVersion 保存完成一类任务的方法和约束，包括说明、适用条件、Prompt 片段以及所需 Tool/Source 的稳定 Key。Skill 不存放商品事实或文档正文；这些内容属于知识库。

Context 常驻提供当前 Agent 的 Skill 名称、描述与版本；模型判断任务相关后调用 Runtime 内置只读 `load_skill`，Runtime 仅从当前 Run 固定的 Skill 版本中加载完整指令，并以事件记录激活事实。Skill 指令影响任务方法，不授予工具权限。

Skill 可以是代码随附的全局内置定义，也可以是 Workspace 编辑的数据化定义；后者不能携带或执行 Python 代码。SkillVersion 发布后不可修改。Run 记录实际使用的 SkillVersion 集合，不通过“当前最新版本”恢复。

### 3.5 SourceDefinition

SourceDefinition 描述能力来源及连接配置引用：

```text
source_id
source_version
kind: LOCAL | MCP
endpoint_or_server_name
secret_id
enabled_tools
health_policy
```

SourceDefinition 不保存明文凭证。业务平台客户端不是动态 REST Source，而由 `adapters/commerce/` 实现并绑定到本地业务 Tool。

## 4. 启动时组装与注册

Worker 组合根显式构造 `RuntimeAssembly`：

```text
RuntimeAssembly
  ├── AgentRegistry
  ├── ToolRegistry
  ├── BackendRegistry
  ├── RepositorySet
  ├── PreToolUsePipeline
  ├── ContextAssembler
  ├── EventWriter
  └── IntegrationClients
```

Registry 只完成按稳定标识查找，不承担业务决策。

当前产品只发布商品运营 Agent；Workspace 绑定一个已发布版本，用户不在任务入口选择 Agent。Runtime 保留通用 Agent/AgentVersion 数据模型，但不意味着产品支持用户创建或切换多个业务 Agent。

启动校验包括：

- 每个启用的 AgentVersion 都能找到所需 Skill、Tool 和 Source 版本；
- 每个 Local ToolVersion 恰好有一个 ToolBinding；
- ToolDefinition 的 `schema_hash` 与代码注册的 Schema 一致；
- 每个写 Tool 都声明 Operation 与 Reconciliation 能力；
- 当前 AgentBackend 满足 Agent 必需能力；
- 缺少 Handler、版本冲突或重复注册时拒绝启动相关 Agent，不以运行时猜测修复。

MCP Tool 在 Source 同步时生成不可变 ToolVersion；MCP Server 返回的 Schema 发生变化时创建新版本，不能覆盖正在运行的版本。

MCP Server 只提供远端工具名称、描述和 Schema，不能自行决定本地 `effect`、动作权限或是否需要确认。每个 MCP Tool 必须经过本地受信任配置绑定副作用分类与权限；未分类工具默认禁用。Schema 或描述变化后，新 ToolVersion 必须重新完成本地分类才能启用。

商品平台 REST API 不作为动态 `Source`：商品 `Local Tool` 通过 `CommerceAdapter` 调用固定业务端点。REST Adapter 只接受组合根配置的 HTTPS Base URL 与主机 allowlist，不接受模型提供 URL/方法；MCP Tool 仍由 Source 装载。

## 5. AgentBackend 接口

Runtime 只依赖统一 `AgentBackend`：

```text
capabilities() -> AgentBackendCapabilities
stream(BackendRequest, CancellationToken) -> AsyncIterator[BackendEvent]
```

`BackendRequest` 包含：

- 已完成的标准化 Message；
- 当前 AgentVersion 和 Prompt 版本；
- 本 Run 固定的模型配置版本；
- 当前可见 ToolDefinition；
- 可选的 Structured Output Schema；
- Provider 无关的生成参数和追踪上下文。

标准化 `BackendEvent` 只允许：

```text
TextDelta
ToolCallDelta
ToolCallCompleted
UsageReported
BackendCompleted
BackendFailed
```

ToolCall 只有收到 `ToolCallCompleted` 且参数 JSON 可解析后才能进入 Runtime。未完成的 ToolCallDelta 永远不能触发 Tool。

`OpenAIBackend` 与 `AnthropicBackend` 分别负责：

- SDK 和流协议适配；
- Tool Calling 格式归一化；
- Provider 错误映射；
- Usage 归一化；
- 取消能力和能力声明。

Runtime 不根据厂商名称写特殊分支。DeepSeek 等 OpenAI 兼容服务通过 `OpenAIBackend` 使用自定义 Base URL。

## 6. Message 与流式输出

Message 类型包括 `USER`、`ASSISTANT`、`TOOL` 和 `SESSION_SUMMARY`。用户消息在创建 Run 前已完整持久化。`SESSION_SUMMARY` 是 Runtime 内部类型，转换为 BackendRequest 时作为带明确边界的非指令数据，而不是 Provider 的 System Message，避免把旧用户输入或 Tool 内容提升为系统规则。

Assistant Message 状态：

| 状态 | 含义 |
|---|---|
| `STREAMING` | 正在接收 Provider 输出 |
| `COMPLETED` | 输出完整，可进入后续 Context |
| `ABORTED` | Provider 失败、取消或 Worker 丢失，内容仅供诊断 |

只有 `COMPLETED` 的 Assistant、Tool 和 Summary Message 可以进入后续 Context。恢复时不得把半截流式文本当成模型已完成决定。

流式文本写入 Message 累积内容，并以合并后的 AgentEvent 推送；不要求每个 token 单独落库。WebSocket 断线后，客户端先读取当前 Message 快照，再按 Event cursor 重放后续事件。

Provider 在完成前失败时，当前 Assistant Message 标记为 `ABORTED`。只有错误被分类为可重试、尚未产生完整 ToolCall、未超过模型调用预算时，才创建新 RunAttempt 继续同一 Run。

事务边界：`Runtime` 不用单事务包住整个 `AgentLoop.advance`。状态迁移与对应 AgentEvent 在短事务内一同提交；请求模型前先提交准备状态，流式消息按合并块写入并提交后才等待下一段 Provider 输出。进程在流中途退出时，恢复推进会先将遗留 `STREAMING` Message 标为 `ABORTED`，部分输出不进入 Context。

## 7. ContextAssembler

ContextAssembler 以固定优先级构造 BackendRequest：

1. Runtime 安全规则和 Agent System Prompt；
2. 当前 AgentVersion 与已启用 SkillVersion 指令；
3. Workspace 非敏感配置摘要；
4. Session 中已完成的消息和持久化 Summary；
5. 当前 Run 已完成的 ToolResult、拒绝结果和交互结果；
6. 经过过滤的长期记忆；
7. 当前用户消息。

安全和当前用户意图不可被压缩掉。上下文超出预算时，优先丢弃低相关长期记忆和旧 ToolResult，再对较早的已完成对话生成持久化 Summary。

长期记忆和外部知识都是不可信数据，不是系统指令。Memory 内容以带来源的数据块注入；WeKnora 知识通过显式检索 Tool 获取并保留文档来源，不把整库内容自动拼进 System Prompt。

MCP、平台 API 和其他 ToolResult 同样视为不可信数据，只能作为 `TOOL` Message 回填，不能升级为 System Message 或修改 Runtime 规则。ToolResult 的结构化字段按本地 Output Schema 过滤，未知字段不进入后续 Context。

ContextAssembler 不读取 Secret 明文，不把权限快照当成当前授权事实。

`ContextBudgetPolicy` 使用模型配置中的 Context Window、最大输出 Token、Tool Schema Token 和安全余量计算本轮输入预算。安全规则、当前用户消息、活动 ToolCall、等待中的 Input/Confirmation 和恢复所需事实属于 Protected Context，不能被裁剪或摘要。预算不足时按以下顺序收敛：低相关长期记忆、旧 ToolResult Context 投影、较早已完成消息。

仍然超预算时，`ConversationSummarizer` 使用当前 Run 固定的 AgentBackend、关闭 Tool Calling，并按版本化 Schema 生成结构化 Summary Message，保存 `covered_from_sequence / covered_to_sequence / summary_schema_version / source_hash`。Summarizer 每次只处理能进入独立 Summary 输入预算的最早连续消息，可将上一版 Summary 与下一段消息增量合并，不能把全部超长历史再次塞入一次请求。该调用计入 Run 的模型调用预算；输出 Schema 校验失败时不持久化 Summary。Summary 只能覆盖已完成且不再活动的消息；生成失败时不得静默丢失 Protected Context，若 Protected Context 本身超限则以 `CONTEXT_BUDGET_EXCEEDED` 终止本次推进。Token 估算由注入的 `TokenEstimator` 完成，Provider 无精确计数接口时使用保守估算并保留安全余量。

## 8. Agent Loop

一次 Worker 推进 Run 的主循环：

```text
加载 Run 与固定版本
  → 重新校验 Actor 和 AgentBackend 能力
  → ContextAssembler 构造请求
  → AgentBackend 流式生成
  → 标准化并持久化完成输出
  → 无 ToolCall：完成 Run
  → 有 ToolCall：按模型顺序逐个进入 PreToolUse
  → 执行或挂起
  → ToolResult 回填后进入下一轮模型调用
```

首版不并行执行 ToolCall。模型一次返回多个 ToolCall 时：

- 先把所有已完成的 ToolCall 按 `ordinal` 持久化为 `PROPOSED`，再开始处理第一个调用；
- 按 Provider 输出中的稳定顺序处理；
- 每个 ToolCall 独立执行 PreToolUse；
- 任一 ToolCall 进入 Input、Confirmation 或 Reconciliation 等待时，暂停后续 ToolCall；
- 恢复后先完成被挂起的 ToolCall，再处理剩余调用；
- 写 Tool 的执行结果可能改变后续调用前提，因此后续调用必须重新读取当前状态。

每个 AgentVersion 必须配置硬限制：

- 最大模型轮次；
- 最大 ToolCall 数；
- 单次 Tool 超时；
- Run 总时限；
- 模型重试次数；
- Reconciliation 次数和时限。

达到限制时 Run 以明确错误码 `LOOP_LIMIT_EXCEEDED`、`TOOL_LIMIT_EXCEEDED` 或 `RUN_DEADLINE_EXCEEDED` 失败，不能继续自动循环。

## 9. PreToolUse 扩展协议

PreToolUse 使用固定阶段，不能由业务 Agent 任意重排：

```text
Tool Resolution
  → Schema Validation
  → Input Normalization
  → Argument Provenance Resolution
  → Input Requirement Resolution
  → Authorization Resource Resolution
  → Access Control
  → Business Policy
  → Execution Mode
  → Confirmation Verification
```

统一决策类型：

```text
ALLOW
DENY(reason_code, safe_message)
REQUIRE_INPUT(InputRequirement)
REQUIRE_CONFIRMATION(ConfirmationSpec)
```

各阶段职责：

- Tool Resolver：只根据 Run 快照解析已固定的 ToolVersion，未知 Tool、版本漂移或已禁用版本直接拒绝；
- Runtime Schema Validator：使用已解析 ToolVersion 的 JSON Schema 完成 JSON 解析、类型和结构校验；
- InputNormalizer：由业务 Agent 注册确定性归一化逻辑，处理别名、ID 格式和单位，不进行模型猜测；
- ArgumentProvenanceResolver：根据当前用户消息、已回答 Request 和权威读取生成参数来源，不接受模型自报来源；
- InputRequirementResolver：由业务 Agent 注册，把可由用户补充的问题转成 InputRequirement；
- AuthorizationResourceResolver：由业务 Agent 从规范化参数提取 `(resource_type, resource_id)`，不读取或修改授权结果；
- Access Control：Runtime 根据 ToolDefinition 的动作权限和解析后的资源引用调用统一授权服务，不接受模型自报权限；
- Business Policy：业务 Agent 以结构化 PolicyDecision 返回允许或拒绝；
- Execution Mode：Runtime 决定只读、确认或自动执行；
- Confirmation Verification：Runtime 验证指纹、Actor、过期时间和当前状态。

业务扩展返回数据，不直接修改 Run、ToolCall 或 Request 状态。只有 Runtime 应用服务能够执行状态迁移并写 AgentEvent。

Schema Validator 不把所有校验错误都等价处理：

- ToolCall 参数不是可解析 JSON、出现未允许字段或根结构错误时，按 `INVALID_MODEL_OUTPUT` 处理，不向用户询问技术性问题；
- 必填业务字段缺失或值不完整时，Schema Validator 生成结构化诊断，由 InputRequirementResolver 决定能否转换为用户可回答的 Input Request；
- InputNormalizer 只处理已存在且结构可用的字段，不通过默认值隐藏缺失的关键业务作用域。

写 Tool 在所有必需参数已完成规范化、且首次进入 Confirmation 或执行边界前，由 Runtime 在 ToolCall 上分配稳定 `operation_id`。该 ID 在创建 Confirmation Request 前已存在，但不代表已发起外部写入；真正执行时才以同一 ID 创建或读取 `PREPARED` Operation。

## 10. ToolHandler 与结果模型

ToolHandler 接收已通过 PreToolUse 的不可变 `ToolExecutionContext`：

```text
workspace_id
session_id
run_id
tool_call_id
operation_id
actor_id
normalized_arguments
deadline
trace_context
```

Runtime 写操作按两阶段事务执行：先提交 `Operation(EXECUTING)`、Attempt 与授权审计，再在无 Runtime 数据库事务期间调用平台 Adapter；回执经过 PostToolUse 后，在新的短事务中提交 Operation/ToolCall/Run 状态与事件。读 Tool 请求和平台回查同样在 Runtime 事务外等待网络。长调用由 `WorkerHost` 心跳续租；每个 Runtime 提交检查点仍校验 lease token，失租执行不得继续持久化。

ToolHandler 返回标准化 ToolResult：

```text
status: SUCCEEDED | FAILED | UNKNOWN
content
structured_data
error_code
retry_class: NEVER | TRANSIENT | RECONCILE_FIRST
external_reference
observed_state
```

ToolHandler 不自行重试写操作、不创建 Request、不提交 Runtime Repository，也不向前端直接推送事件。重试和状态转换由 Runtime 根据 ToolResult 与 Adapter 能力决定。

对外展示的 `safe_message` 与内部诊断分离；异常堆栈、凭证、请求 Header 和未脱敏响应不得进入模型上下文。

### 10.1 固定 PostToolUse 结果管线

ToolHandler 返回后统一进入 `ToolResultProcessor`，阶段固定为：

```text
Result Protocol Validation
  → Operation Outcome Classification
  → Output Schema Validation
  → Redaction
  → Size Policy
  → Context Projection
  → State / AgentEvent Persistence
```

- Result Protocol Validation 校验 `status/retry_class/external_reference/observed_state` 的组合是否合法；
- Operation Outcome Classification 先保留外部副作用事实。写操作结果不能因后续 Schema 或投影失败被降格为 `NOT_APPLIED`；不能确定时进入 `UNKNOWN` 与 Reconciliation；
- Output Schema Validation 只允许 ToolVersion 声明字段进入结构化结果，未知字段和类型错误形成 `INVALID_TOOL_RESULT`；
- Redaction 在内容写入 Message、AgentEvent、Trace 或日志前执行；
- Size Policy 分别限制权威结果和模型投影大小。首版不建设 Artifact Store，超出权威结果上限时返回确定错误，Context 投影按 Schema 字段和数组上限做确定性裁剪并标记 `truncated/content_hash`；
- Context Projection 只产生不可信的 `TOOL` Message，不能生成 System 指令；
- 状态、脱敏结果与 AgentEvent 在同一事务内提交。

PostToolUse 不是可任意注册代码的 Hook 平台。业务 Agent 可以随 ToolBinding 提供纯数据式 Context Projection 配置，但不能改变 Operation、权限、重试和持久化语义。

## 11. 错误分类与重试归属

错误分类至少包括：

| 类型 | 默认处理 |
|---|---|
| `INVALID_MODEL_OUTPUT` | 在模型预算内重试或失败，不执行 Tool |
| `BACKEND_TRANSIENT` | 同一 Run 新建 Attempt 重试 |
| `BACKEND_PERMANENT` | Run 失败 |
| `VALIDATION_ERROR` | 业务可补充则 Input Request，否则回填安全错误 |
| `ACCESS_DENIED` | ToolCall `DENIED`，不重试 |
| `POLICY_DENIED` | ToolCall `DENIED`，不重试 |
| `TOOL_TRANSIENT_READ` | 允许按策略重试只读 Tool |
| `TOOL_PERMANENT` | ToolCall 失败 |
| `WRITE_OUTCOME_UNKNOWN` | 进入 Reconciliation，禁止直接重试 |

AgentBackend 只负责 Provider 调用级错误映射；ToolHandler 只负责执行并报告；Runtime 是唯一的重试决策者。

## 12. 取消与恢复

CancellationToken 从 Run 传入 AgentBackend 和支持取消的 ToolHandler。取消只在安全点生效：

- 模型流取消后，Assistant Message 标记为 `ABORTED`；
- 未发出的 ToolCall 可以取消；
- 已发出的外部写请求不能假定取消成功，必须进入回查；
- 恢复时加载持久化 Message、ToolCall、Request 和版本快照，不依赖进程内对象。

Handler、Resolver 和 Policy 必须是无进程本地会话依赖的可重建组件。任何恢复所需事实都必须持久化。

## 13. Eval 与验收

Runtime 核心接口使用 `ScriptedBackend`、确定性 ToolHandler 和故障注入测试，至少覆盖：

1. Runtime 包不导入上层业务模块；
2. 数据定义不包含 Python 回调或 Secret；
3. 启动时发现缺失 Handler、Schema 不一致和能力不足；
4. 未完成 ToolCallDelta 不触发 Tool；
5. `ABORTED` Message 不进入 Context；
6. 多 ToolCall 稳定顺序执行，等待时暂停后续调用；
7. 达到 Agent Loop 限制后确定失败；
8. PreToolUse 各阶段顺序固定，归一化和参数来源判定只能使用确定性规则，业务扩展不能直接修改状态；
9. ToolHandler 不自行重试写操作；
10. 外部知识和 Memory 不能覆盖系统规则；
11. 切换 OpenAIBackend 与 AnthropicBackend 不修改业务 Agent 代码；
12. Provider 缺少业务 Agent 必需能力时 Run 创建失败。
13. Context 超预算时按固定优先级收敛，Protected Context 不丢失，Summary 可追溯；
14. ToolResult 协议或输出 Schema 非法时不会污染 Context，Secret 在持久化前被脱敏；
15. 已发生写入的 ToolResult 即使投影失败也不会被解释为未执行或直接重试。
