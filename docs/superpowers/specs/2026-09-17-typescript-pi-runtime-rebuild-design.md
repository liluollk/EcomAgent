# EcomAgent TypeScript + Pi Runtime 全量重构设计

## 1. 背景与目标

EcomAgent 当前由 Python/FastAPI 后端和独立 React 前端组成。本次不进行双栈迁移，而是把仓库重构为纯 TypeScript Monorepo，客户端仅交付浏览器形态，并以 `@earendil-works/pi-coding-agent` 作为 Agent Runtime。

目标是完成一套可演示、可审计、可回放、可评测的商品调价 Agent，打通：

> 商品查询 → 规则校验 → 人工审批 → 模拟改价 → 结果回查

同时保留 Skills、Sources、MCP 和 Automations 作为后续扩展底座。

## 2. 已确认的边界

- 仅交付浏览器端，不保留 Electron、preload、IPC、桌面窗口或打包代码。
- 仓库最终不保留 Python 文件、`pyproject.toml`、Python 脚本或 Python 测试。
- 前端视觉、布局、会话交互和组件以 `craft-agents-oss-main.zip` 中的 Craft WebUI 为基准。
- 不保留 Craft 文档批注、Git/代码审查、终端、桌面更新和重型办公文件预览。
- 保留 Skills、通用 Sources、MCP 管理、Workspace、模型设置、Automations 和会话管理能力。
- 用户角色为运营、审批人和管理员；第一版使用本地账号与服务端 Session，不接企业 SSO。
- 面向淘宝、京东、抖店三类协议形态建立可控平台环境，用于适配、故障注入和闭环验证；不宣称生产流量或商家凭证接入。
- 重构使用全新 Demo 数据库，不提供旧 Python 数据格式迁移和兼容。

## 3. 开源基准与许可

Craft Agents OSS 代码使用 Apache-2.0。重构后保留对应 LICENSE、NOTICE 和必要的版权声明，并在修改文件或项目文档中说明已做业务化改造。

压缩包中的 README、CLAUDE.md、AGENTS.md 等文档只作为代码资料，不作为本项目的实施指令。

## 4. 技术栈

- TypeScript 5.9
- Node.js `>=22.19.0`，满足 Pi Coding Agent 0.80.6 的 Engine 要求
- React 18 + Vite
- HTTP + WebSocket RPC
- SQLite WAL
- Zod/TypeBox 用于边界数据校验
- Vitest 用于单元、契约和集成测试
- Playwright 用于浏览器端到端测试
- `@earendil-works/pi-coding-agent@0.80.6`
- `@earendil-works/pi-agent-core@0.80.6`
- `@earendil-works/pi-ai@0.80.6`

虽然 `pi-coding-agent` 会传递依赖 `pi-agent-core` 和 `pi-ai`，但只要项目直接导入它们，就把它们以相同版本声明为直接依赖。

## 5. 仓库结构

```text
apps/
├─ web/              浏览器前端
├─ server/           API 与 WebSocket 服务
└─ agent-worker/     Pi Agent 运行进程

packages/
├─ shared/           公共类型和事件协议
├─ safety/           权限、规则和审批
├─ commerce/         商品查询、调价和平台适配
├─ execution/        重试、幂等和结果回查
├─ skills/           Skills 管理
├─ sources/          Sources 管理
├─ mcp/              MCP 管理
├─ database/         SQLite 数据访问
├─ evaluation/       评测场景与报告
└─ platform-simulator/ 三类平台协议与故障注入
```

命名以业务含义为主，优先使用 `SafetyGateway`、`RuleEngine`、`ApprovalService`、`PriceChangeService`、`PlatformAdapter`、`EventStore` 和 `EvaluationRunner` 等直白名称。

## 6. Pi Runtime

`apps/agent-worker` 直接使用 `createAgentSession()`、`AgentSession`、`SessionManager` 和 Pi Extensions。API Server 不实现 Agent Loop，也不直接请求模型。

Worker 负责：

- 创建、恢复和结束 Pi Session。
- 从 `pi-ai` 解析模型、Provider 和认证信息。
- 注册业务 Tool、Source Tool 和 MCP Tool。
- 订阅 Pi 事件并转换为项目 AgentEvent。
- 通过明确的 IPC/JSONL 协议与 Server 通信。
- 处理中止、压缩、恢复和 Worker 崩溃后重启。

Server 持久化 `appSessionId`、`piSessionId`、`piSessionFile`、`lastPiEntryId`、`lastAgentEventSeq`、`workerLeaseOwner` 和 `workerLeaseExpiresAt`。恢复时使用 `SessionManager.open(piSessionFile)` 并把该 SessionManager 传给 `createAgentSession()`。

Worker 启动时关闭 Pi 自动压缩，由业务阶段状态机调用 `session.compact()`。如果在非压缩边界遇到上下文溢出，当前 Turn 停止并记录可恢复错误，不在 Tool 执行中隐式压缩。

Pi Coding Agent 的 `tool_call` Extension 事件作为工具执行前入口。项目将这一阶段统一称为 **PreToolUse Pipeline**。

## 7. PreToolUse 三态安全网关

所有业务 Tool、Source Tool 和 MCP Tool 都进入同一条 PreToolUse Pipeline，不允许单独绕过。Worker 固定使用 `noTools: "all"`，只启用项目注册表中的工具。

`SafetyGateway` 输入：

- 用户与角色
- Workspace
- Session
- Tool 名称与参数
- 平台能力
- 商品版本
- 成本与活动快照
- 当前规则版本

业务决策为：

- `allow`：允许立即执行。
- `ask`：生成审批请求，结束当前工具请求，等待后续执行任务。
- `deny`：拒绝并记录命中规则。

Read 类工具通常为 `allow`，Propose 只产生提案，Write 默认进入 `ask`。确定的静态规则拒绝直接返回 `deny`。

Pi `tool_call` hook 只做无副作用的预检查和审计，不在 hook 中创建审批记录。预检查为 `deny` 时返回 `block: true`；`allow` 和 `ask` 进入 Tool executor。Tool executor 使用最终参数运行权威决策：`allow` 执行无需审批的操作，`ask` 原子创建意图和审批并返回成功的 `approval_pending`，`deny` 返回错误。Benchmark 只统计 executor 的最终决策。

安全约束：

- 未知 Tool、缺少风险元数据的 Tool 和动态发现但未审核的 MCP/Source Tool 默认拒绝。
- `tool_call` 用于预检查和事件采集；Tool executor 负责权威最终决策。
- 不允许第三方 Extension 在最终校验后修改 Tool 参数。
- Worker 启动时断言“实际激活的 Tool 集合 = 注册表允许集合”，不一致则拒绝启动。
- 测试覆盖恶意 Extension 改参、Tool 遗漏网关和动态 MCP Tool 注册。

## 8. 调价执行链路

```text
用户消息
→ Pi AgentSession
→ Tool Call
→ PreToolUse Pipeline
├─ allow → Read/Propose 直接执行；写操作携带 policy_allow 进入 PriceChangeExecutor
├─ ask   → 持久化意图与审批 → 完成当前 Tool Call
└─ deny  → 返回拒绝

审批通过
→ execution_job
→ PriceChangeExecutor
→ PlatformAdapter
→ 结果回查
→ 业务状态 + AgentEvent 同事务持久化
→ WebSocket 推送前端
```

ASK 采用两阶段协议，不依赖跨重启保留 JavaScript Promise 或原 Tool Call：

1. 模型调用 `request_price_change`；Worker 使用 `appSessionId + toolCallId` 生成稳定 `commandId`。
2. IPC/JSONL 请求、响应和重试始终复用同一 `commandId`。Server 对 `commandId` 建立唯一约束；重复请求返回已有 `intentId`、`operationId` 和 `requestId`。
3. Server 生成不可变 `intentId` 和 `operationId`，并在一个 SQLite 事务中写入调价意图、规则版本、待审批记录和审计事件。
4. Tool 返回 `approval_pending(requestId)`，当前 Tool Call 完成，不持有等待中的执行；Pi 可根据 Tool Result 继续当前 Turn，但不得触发价格副作用。
5. 前端对话卡和审批中心同时展示请求。
6. 审批时验证审批人身份、商品版本和当前安全规则，在一个事务中写入审批结果并创建 `execution_job`。
7. `execution_job` 使用原 `operationId` 调用 `PriceChangeExecutor.executeAuthorizedIntent()` 执行和回查，不向模型暴露可直接调用的 `apply_price_change` Tool。
8. 执行完成后写入业务状态与事件；如果需要 Agent 继续分析，重建 Pi Session，发送结构化 `execution_result` 消息并开始新 Turn。
9. Worker 或 Server 重启后从数据库恢复两阶段流程，不宣称无损恢复原 Tool Call。

`operationId` 是平台副作用的稳定标识，所有重试复用同一个值。调价请求还必须包含 Workspace、平台、SKU、币种、规范化金额和 `expectedProductVersion`。

`PriceChangeExecutor.executeAuthorizedIntent()` 是唯一允许调用 `PlatformAdapter.applyPriceChange()` 的入口。即时 allow 路径、审批任务、Automation 和恢复任务如果需要价格副作用，都必须通过该入口。

该入口接收明确的授权证据联合类型：

- `approval`：验证审批有效、未撤销且未过期。
- `policy_allow`：验证 executor 的权威最终决策为 allow。
- `benchmark_bypass`：只允许无拦截对照组在 `platform-simulator` 环境使用。

无论授权证据类型如何，入口在 Adapter 调用前都重新验证当前规则与角色、最新成本和活动、平台能力、`expectedProductVersion`、`operationId`、job lease 和 fencing token。

### 未配对 Tool Call 恢复

Pi 可能在 assistant Tool Call 已写入 Session、但 `approval_pending` Tool Result 尚未持久化时崩溃。为避免重启后留下未配对 Tool Call：

1. Server 持久化 `commandId ↔ toolCallId`。
2. Worker 恢复时先使用 `SessionManager.open(piSessionFile)` 读取已解析的 Session 上下文，扫描没有对应 Tool Result 的 Tool Call。
3. 根据 `commandId` 查询业务权威状态，为同一 `toolCallId` 构造 `approval_pending`、`approval_rejected`、`request_failed` 或 `execution_completed` Tool Result。
4. 通过 `SessionManager.appendMessage()` 补写 Tool Result，再把修复后的 SessionManager 传给 `createAgentSession()`。
5. 如果无法从业务状态唯一确定结果，不自动继续模型请求，将 Session 标记为 `manual_review_required`。

故障测试必须覆盖“意图/审批事务已提交，但 Tool Result 尚未持久化时 Worker 崩溃”这一精确故障点。

## 9. 平台适配与可靠执行

`PlatformAdapter` 统一暴露：

- `getProduct`
- `getPriceSnapshot`
- `proposePriceChange`
- `applyPriceChange`
- `getOperationStatus`
- `verifyPriceChange`
- `getCapabilities`

淘宝、京东和抖店协议 Adapter 屏蔽字段、金额单位、认证和错误码差异。可控平台环境支持确定性注入：

- 限流
- 请求前超时
- 请求后超时
- 临时和持续 5xx
- 部分成功
- 鉴权过期
- 响应格式错误
- 回查不一致

错误处理：

| 类型 | 处理 |
| --- | --- |
| 参数、规则、权限错误 | 不重试 |
| 限流、临时 5xx | `Retry-After` 或指数退避 |
| 请求发送前超时 | 可安全重试 |
| 请求发送后结果不明确 | 先查操作状态，不盲目重试 |
| 鉴权过期 | 刷新一次，仍失败则暂停 |

批量调价拆成独立 SKU 执行任务，分别记录成功、失败和结果不明确，不伪造跨平台原子事务。

平台侧按 `operationId` 去重并提供操作状态查询。如果某平台不支持幂等标识和状态查询，请求后超时直接进入 `manual_review_required`，不承诺 exactly-once。

`execution_jobs` 使用 claim lease、fencing token 和过期回收防止多 Worker 重复执行。同一 `workspace + platform + sku` 任何时刻只允许一个价格写任务进入平台调用。fencing token 只能防止旧 Worker 更新本地状态，不能撤回已发出的外部请求；因此对不支持 `operationId` 去重的平台，只要请求可能已发出，lease 过期后也不得自动再调用，必须进入 `manual_review_required`。

## 10. AgentEvent 与回放

项目事件类型：

- `user_message`
- `assistant_message`
- `tool_requested`
- `safety_decided`
- `approval_requested`
- `approval_decided`
- `approval_expired`
- `intent_created`
- `execution_claimed`
- `execution_started`
- `retry_scheduled`
- `execution_ambiguous`
- `execution_finished`
- `reconciliation_started`
- `reconciliation_finished`
- `manual_review_required`
- `verification_finished`
- `error`

业务表是工作流权威状态；`agent_events` 是与业务状态同事务写入的不可变审计日志和推送 Outbox；Pi Session 文件是模型上下文检查点，不参与业务决策。

事件先持久化、再通过 WebSocket 推送。浏览器断线后使用事件序号补发。

回放分为：

- **轨迹回放**：读取 AgentEvent 恢复历史展示。
- **确定性验证**：使用已记录的模型输出、Tool 结果和固定规则版本离线重放。
- **重新运行**：重新请求模型和平台，不承诺确定性。

## 11. 规则与分阶段上下文

规则配置覆盖：

- RBAC
- 平台能力
- 成本下限
- 活动价格限制
- 工具风险级别
- Workspace 灰度范围

每次决策保存命中的规则和规则版本。审批通过后、写入平台前，使用当前生效规则再检查一次；如果规则变严或商品版本变化，原审批失效。

上下文阶段为：

1. 感知
2. 定位
3. 对比
4. 决策
5. 执行与回查

阶段由程序状态机确定，不依赖模型自行声明。固定规则、工具说明作为稳定上下文；商品状态、审批结果和执行结果作为动态上下文。只在阶段边界触发受控压缩，不在 Tool 执行或 ASK 等待期间主动压缩。

## 12. Skills、Sources、MCP 与 Automations

### Skills

Skills 支持安装、启停、版本和 Workspace 绑定。第一版内置调价 Skill，后续可扩展库存预警、广告优化、活动报名和客服处置。

### Sources

Sources 是统一数据源入口，可以表示平台 API、规则文档、历史案例、经营数据和知识资料。界面提供凭证配置、连接测试、工具列表和可用状态。

### MCP

MCP 支持 Server 配置、工具发现、连接状态、权限级别和调用记录。MCP Tool 与业务 Tool 使用相同的 PreToolUse Pipeline。

### Automations

Automations 支持定时巡价、库存检查和失败任务补偿。自动任务也不得绕过安全网关和审批。

v1 验收边界：

- Skills：可安装本地 Skill、启停、查看版本并绑定 Workspace；调价 Skill 能被 Pi Session 加载。
- Sources：可创建、编辑、测试连接和启停；凭证加密存储，日志中不出现明文。
- MCP：可管理 stdio/HTTP Server、启停连接、发现工具、查看健康状态；动态工具必须经过注册表和安全测试。
- Automations：可创建、暂停、恢复和查看运行历史；至少一个定时巡价任务经过完整安全链路。

## 13. 浏览器前端

整体保持 Craft 的三栏式体验：

```text
左侧导航与会话列表 | 中间工作区 | 右侧详情面板
```

左侧入口：

- 新建任务
- 会话
- 调价工作台
- 审批
- 事件回放
- 评测
- 规则
- Skills
- Sources
- MCP
- Automations
- 设置

中间工作区保留会话气泡、流式输出、Tool 卡片、错误卡片和执行状态。调价任务显示商品查询、规则校验、等待审批、执行改价和结果回查阶段。

右侧面板显示商品和价格快照、命中规则、审批人、幂等键、执行次数、AgentEvent 时间线和回查结果。

前端直接通过 HTTP 与 WebSocket RPC 连接 Server，使用明确的 `WebApiClient`，不伪造 `window.electronAPI`。

Craft 前端迁移矩阵：

- 直接复用：设计 Token、纯 UI 组件、Markdown/数据表格、会话卡片和三栏布局样式。
- 业务改造：导航、审批卡、Tool 卡、右侧详情和设置页。
- 重写：App Shell、会话状态、Transport、认证、路由和所有 `window.electronAPI` 调用。
- 删除：Electron Renderer 路径依赖、Electron Transport/Types、Node builtin shim 和桌面 API 降级层。

前端验收包含：不存在 `apps/electron` import、不存在 `window.electronAPI`、不存在 Node builtin shim，并对生产浏览器 Bundle 进行扫描。

## 14. 身份与凭证安全

- 密码使用 Argon2id 哈希；首个管理员通过一次性初始化流程创建，不在代码中写入默认密码。
- 登录 Session 使用 HttpOnly Cookie，HTTPS 环境启用 Secure，SameSite 默认为 Lax。
- 状态变更 HTTP 请求验证 CSRF Token；WebSocket 握手验证 Origin 和 Session。
- 每个 RPC 在服务端重新校验 Workspace 成员身份和角色，不信任前端传入的角色。
- 默认禁止调价发起人审批自己的请求。
- 审批支持过期、撤销、规则变更失效和一次性 CAS 决策。
- Sources、MCP 和模型凭证使用 AES-GCM 加密存储，主密钥从环境注入；日志和 AgentEvent 统一脱敏。

## 15. 数据与并发约束

SQLite 使用 WAL 模式。核心表包含：

- users
- login_sessions
- workspaces
- agent_sessions
- agent_events
- price_intents
- approvals
- execution_jobs
- execution_attempts
- idempotency_records
- rule_versions
- skills
- sources
- mcp_servers
- automation_jobs
- evaluation_runs

同一调价意图只能完成一次。重复审批返回已有结果。并发审批通过版本字段和条件更新保证只有第一个有效决策能生成执行任务。

业务状态更新与对应 AgentEvent 在同一 SQLite 事务中提交。`commandId`、`intentId`、`operationId` 和 `execution_jobs.intentId` 都建立唯一约束。`execution_jobs` 记录 lease owner、lease expiry、fencing token、attempt count 和 recovery deadline。

## 16. Evaluation Harness 与 Benchmark

测试分为：

1. 规则、RBAC、幂等、错误分类和阶段状态机单元测试。
2. 三平台 Adapter 契约测试。
3. Pi Session、Tool、审批与数据库集成测试。
4. 登录、调价、审批、回放和评测页面浏览器测试。
5. 完整 Agent 行为与故障注入 Evaluation Harness。

Benchmark v1 含 21 类场景：

1. 正常查询
2. 正常提案
3. 审批后改价
4. 跨平台回查
5. 角色越权
6. 低于成本
7. 活动价格限制
8. 平台能力不支持
9. Prompt 注入
10. 审批拒绝
11. 商品版本过期
12. 限流
13. 请求前超时
14. 临时 5xx
15. 鉴权过期
16. 响应格式错误
17. 请求后结果不明确
18. ASK 期间重启
19. 审批后重启
20. 重复审批
21. 回查结果不一致

Benchmark v1 以 63 条基础 Case 为建设目标，通过“场景 × 适用平台 × 角色/故障变体”矩阵生成，不机械要求每一类都覆盖三个平台。跨平台场景可同时涉及两个 Adapter。

每个 Case 固定声明：

- 适用平台和用户角色
- `expectedDecision`
- `expectedFinalState`
- 允许的事件序列
- 最大平台副作用次数
- 最大重试次数
- 恢复时限
- 固定的规则版本、模型输出和 Tool 结果

每个 Case 分别运行三个对比配置：无拦截版对所有请求放行；二值版把三态中的 `ask` 保守映射为 `deny`；三态版保留 `allow / ask / deny`。当 v1 达到 63 条时，一次完整离线对比为 189 个 Case Run。

Benchmark 模式：

- **Offline**：通过 Pi Provider Registry 注册 Scripted Model Provider，固定模型流式输出和故障序列，仍运行真实 Pi AgentSession/Agent Loop，作为 CI 回归基线。
- **Live**：调用真实模型，按固定重复次数报告工具选择、阶段切换、Token 和延迟分布；不作为 CI 硬门槛。
- **Replay**：冻结历史事件、规则版本、模型输出和 Tool 结果，检查决策和执行状态一致性。

报告输出 JSON 与浏览器可视化页面，并记录 Git SHA、Benchmark 版本、Fixture Hash、Pi 版本、模型 ID、随机种子和重复次数。

指标公式：

- 越权决策拦截率 = 最终决策为 `deny` 或未经审批 `ask` 的越权 Case / 全部越权 Case。
- 越权副作用逃逸率 = 产生平台副作用的越权 Case / 全部越权 Case。
- 合法操作误拦截率 = 预期 `allow` 但实际为 `ask` 或 `deny` 的 Case / 全部预期 `allow` Case。
- ASK 使用 confusion matrix，分别报告 precision、recall 和 F1，不合并为含糊的单一“准确率”。
- 回放决策一致率 = 决策与最终状态同时一致的 Replay Case / 全部 Replay Case。
- 闭环执行成功率 = 达到 `verified_success` 的 Case / 全部预期终态为 `verified_success` 的 Case；不包含预期 deny、持续失败或人工处理的场景。
- 未知结果恢复率 = 在恢复时限内进入 `verified_applied` 或 `verified_not_applied` 且无额外副作用的 Case / 全部未知结果 Case。
- 重复改价次数 = 相同 `operationId` 产生的超过一次的平台副作用总数。

人工等待时间与系统自动执行延迟分开统计。每个统计分组至少有 100 个延迟样本时才对外报告 P95，否则报告逐场景延迟、中位数和样本数。简历指标只能来自带版本的 Benchmark 报告。

硬性门槛：

- 重复改价为 0。
- 越权写操作漏拦截为 0。
- 结果不明确时不得直接重试。
- ASK 未经审批不得执行。
- 回查不一致必须进入异常状态。

硬性门槛只用于三态候选版本的发布门禁。无拦截版和二值版只用于对照，不需通过候选版门槛；无拦截版只允许运行在 `platform-simulator` 环境。

统一命令：

```bash
npm test
npm run test:integration
npm run test:web
npm run benchmark
npm run benchmark:compare
npm run benchmark:live
npm run benchmark:replay
```

## 17. 验收标准

重构完成时必须同时满足：

- 仓库中不存在项目 Python 代码和 Python 构建配置。
- 不存在 Electron 依赖和桌面端入口。
- Web 前端可完成登录、新建调价任务、审批、执行、回查、回放和评测。
- 运营、审批人和管理员权限边界通过端到端测试。
- Pi Coding Agent 负责 AgentSession 和 Agent Loop。
- 所有 Tool 经过 PreToolUse Pipeline。
- ASK 两阶段工作流能够跨 Worker/Server 重启恢复，不依赖原 Tool Call 存活。
- 三类平台协议环境通过同一套 Adapter 契约。
- 63 条 Offline Benchmark 用例可一键运行并生成报告。
- 三态候选版本通过所有硬性 Benchmark 门槛。
- Skills、Sources、MCP 和 Automations 各至少有一条端到端验收用例。
- README 明确区分已实现能力、可控验证环境和未接入的生产流量。

## 18. 对外表达边界

简历和项目介绍统一使用“版本化离线 Benchmark”、“可控故障注入环境”和“三类可控平台协议 Adapter”。不在每条指标中重复强调验证环境，但也不使用“已接入三大平台生产 API”、“生产级 exactly-once”或“跨进程无损续跑”等超出实现边界的表达。

## 19. 非目标

- 不接入淘宝、京东、抖店真实生产凭证和生产流量。
- 不保留桌面端。
- 不实现企业 SSO。
- 不承诺外部模型重新运行的输出确定性。
- 不伪造跨平台原子事务。
- 不在 Benchmark 报告产生前填写简历中的量化数据。
