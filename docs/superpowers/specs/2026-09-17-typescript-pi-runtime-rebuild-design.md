# EcomAgent TypeScript + Pi Runtime 全量重构设计

## 1. 背景与目标

EcomAgent 当前由 Python/FastAPI 后端和独立 React 前端组成。本次不进行双栈迁移，而是把仓库重构为纯 TypeScript 的浏览器应用，并以 `@earendil-works/pi-coding-agent` 作为 Agent Runtime。

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
- 电商平台为淘宝、京东、抖店三个模拟平台，不宣称真实生产平台接入。

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
└─ mock-platforms/   三个模拟电商平台
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

Pi Coding Agent 的 `tool_call` Extension 事件作为工具执行前入口。项目将这一阶段统一称为 **PreToolUse Pipeline**。

## 7. PreToolUse 三态安全网关

所有业务 Tool、Source Tool 和 MCP Tool 都进入同一条 PreToolUse Pipeline，不允许单独绕过。

`SafetyGateway` 输入：

- 用户与角色
- Workspace
- Session
- Tool 名称与参数
- 平台能力
- 商品版本
- 成本与活动快照
- 当前规则版本

输出为：

- `allow`：允许立即执行。
- `ask`：生成审批请求并挂起执行。
- `deny`：拒绝并记录命中规则。

Read 类工具通常为 `allow`，Propose 只产生提案，Write 默认进入 `ask`。确定的静态规则拒绝直接返回 `deny`。

## 8. 调价执行链路

```text
用户消息
→ Pi AgentSession
→ Tool Call
→ PreToolUse Pipeline
→ allow / ask / deny
→ PlatformAdapter
→ 结果回查
→ AgentEvent 持久化
→ WebSocket 推送前端
```

ASK 流程：

1. 在一个 SQLite 事务中写入调价意图、规则版本、待审批记录和幂等键。
2. 前端对话卡和审批中心同时展示请求。
3. 审批时验证审批人身份、商品版本和当前安全规则。
4. 在一个事务中写入审批结果并创建执行任务。
5. 执行结果恢复给等待中的 Pi Session。
6. Worker 或 Server 重启后，根据数据库中的未完成状态继续处理。

幂等键格式为：

```text
sessionId + platform + sku + targetPrice + productVersion
```

## 9. 平台适配与可靠执行

`PlatformAdapter` 统一暴露：

- `getProduct`
- `getPriceSnapshot`
- `proposePriceChange`
- `applyPriceChange`
- `getOperationStatus`
- `verifyPriceChange`
- `getCapabilities`

淘宝、京东和抖店 Mock Adapter 屏蔽字段、金额单位、认证和错误码差异。Mock 平台支持确定性注入：

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

## 10. AgentEvent 与回放

项目事件类型：

- `user_message`
- `assistant_message`
- `tool_requested`
- `safety_decided`
- `approval_requested`
- `approval_decided`
- `execution_started`
- `execution_finished`
- `verification_finished`
- `error`

AgentEvent 是前端展示、业务审计、问题定位和回放的唯一事实源。Pi Session 文件是运行时检查点，不用于业务查询和审计。

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

## 14. 数据与并发约束

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

## 15. Evaluation Harness 与 Benchmark

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

每类在淘宝、京东、抖店上各一条基础用例，共 63 条。对比无拦截、二值拦截和三态网关三组配置，一次完整离线对比为 189 个 Case Run。

Benchmark 模式：

- **Offline**：固定模型输出和故障序列，作为 CI 回归基线。
- **Live**：调用真实模型，统计工具选择、阶段切换、Token 和延迟。
- **Replay**：重放历史 Session，检查规则决策和执行状态一致性。

报告输出 JSON 与浏览器可视化页面。核心指标：

- 越权写操作拦截率
- 合法操作误拦截率
- ASK 决策准确率
- 回放决策一致率
- 闭环执行成功率
- 未知结果恢复率
- 重复改价次数
- 平均与 P95 自动执行延迟
- Token 使用量
- 各平台通过率

人工等待时间与系统自动执行延迟分开统计。简历指标只能来自带版本的 Benchmark 报告。

硬性门槛：

- 重复改价为 0。
- 越权写操作漏拦截为 0。
- 结果不明确时不得直接重试。
- ASK 未经审批不得执行。
- 回查不一致必须进入异常状态。

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

## 16. 验收标准

重构完成时必须同时满足：

- 仓库中不存在项目 Python 代码和 Python 构建配置。
- 不存在 Electron 依赖和桌面端入口。
- Web 前端可完成登录、新建调价任务、审批、执行、回查、回放和评测。
- 运营、审批人和管理员权限边界通过端到端测试。
- Pi Coding Agent 负责 AgentSession 和 Agent Loop。
- 所有 Tool 经过 PreToolUse Pipeline。
- ASK 能够跨 Worker/Server 重启恢复。
- 三个 Mock 平台通过同一套 Adapter 契约。
- 63 条 Offline Benchmark 用例可一键运行并生成报告。
- 所有硬性 Benchmark 门槛通过。
- README 明确区分已实现能力、Mock 验证和未接入的真实平台。

## 17. 非目标

- 不接入淘宝、京东、抖店真实生产凭证和生产流量。
- 不保留桌面端。
- 不实现企业 SSO。
- 不承诺外部模型重新运行的输出确定性。
- 不伪造跨平台原子事务。
- 不在 Benchmark 报告产生前填写简历中的量化数据。
