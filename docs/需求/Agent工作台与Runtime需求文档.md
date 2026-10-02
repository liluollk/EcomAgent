# Agent 工作台与 Runtime 需求文档

> 版本：v0.2  
> 日期：2026-10-02  
> 状态：待评审

## 1. 项目定位

本项目面向电商企业内部商品运营场景，从零设计并实现一套 Agent 工作台及其通用 Agent Runtime。Runtime 统一多模型接入、业务能力装载、Workspace/Session 状态隔离、工具权限与运行状态管理；工作台为运营人员提供任务对话、执行进度、权限确认和中断恢复入口。

首个上层业务 Agent 是商品运营 Agent，覆盖商品查询、维护计划、敏感写操作确认、执行和结果回查。Runtime 的职责与商品业务逻辑解耦，使后续店铺运营、活动运营和客服售后 Agent 可以复用同一套运行底座。

项目的产品主体是 Agent 工作台，Runtime 是其核心技术底座。FastAPI、WebSocket 等属于实现方式，不作为项目定位；Eval 属于工程验证体系，不作为面向用户的产品模块。

## 2. 用户与价值

### 2.1 目标用户

目标用户是多品牌电商企业内部的业务人员，业务场景包括品牌运营、店铺运营和客服售后。首版聚焦其中的商品运营任务，不另外定义 Agent 管理者、研发人员或审计人员等独立产品角色。

### 2.2 核心价值

- 运营人员在统一工作台中使用不同业务 Agent，不需要理解模型和工具接入细节；
- 新业务 Agent 复用 Runtime 的模型、会话、能力、权限和状态管理，不重复建设执行基础设施；
- 高风险工具调用在执行前经过代码侧校验和人工确认，避免仅依赖模型自律；
- 任务中断后可以从持久化 Session 恢复，不需要重新开始整段对话和操作。

## 3. 项目边界

### 3.1 首版范围

- Agent 工作台：Workspace 选择、Session 对话、执行状态、权限请求和恢复入口；
- Agent Runtime：完整执行链、多模型抽象、能力装载、权限治理和状态管理；
- 商品运营 Agent：作为第一个真实上层业务 Agent；
- Source 接入：本地 Tool、REST Adapter 和 MCP Server；
- Eval：分别验证 Runtime 行为合同和商品运营任务结果。

### 3.2 首版不做

- 多租户计费、组织管理、应用市场和云端部署控制面；
- 真实企业账号体系和生产 SLA；
- 同时实现店铺运营、活动运营和客服售后三套业务 Agent；
- 为展示技术栈而引入工作流引擎、向量数据库或多 Agent 协作；
- 将模拟平台结果描述为真实线上业务指标。

## 4. 术语边界

### 4.1 Agent Runtime

Agent Runtime 是业务 Agent 的统一运行底座，负责 Workspace、Session、能力装载、模型调用、工具执行、权限等待、异常处理、状态持久化和 AgentEvent。

### 4.2 Agent Harness

Agent Harness 是 Runtime 内部面向模型的编排部分，负责 Prompt/Context 装配、工具暴露、Agent Loop、Tool Result 回填和上下文压缩。Harness 不是 Eval，也不是整个 Runtime。

### 4.3 Agent 工作台

Agent 工作台是运营人员使用业务 Agent 的产品界面，负责 Workspace 与 Session 导航、任务对话、工具进度、Permission Request、执行状态和恢复操作。工作台通过 Runtime API 和 AgentEvent 工作，不直接调用模型 SDK 或业务工具。

### 4.4 Eval

Eval 是 Runtime 外部的工程验证系统，通过固定场景、Runner 和 Scorer 检查执行结果与轨迹。Eval 不参与生产执行，不称为 Harness。

## 5. 总体架构

```text
Agent 工作台
    ├── Workspace：品牌 / 渠道 / 店铺级配置
    ├── Session：对话 / ToolCall / Permission Request / 执行状态
    └── 商品运营 Agent
                    ↓
              Agent Runtime
    Workspace / Session
        → Skill / Source
        → AgentBackend
        → ToolCall
        → PreToolUse
        → AgentEvent
                    ↓
    Model Provider / REST API / Local Tool / MCP Server
```

Runtime 抽象模型调用、工具执行、权限等待和异常处理；商品运营 Agent 只定义 Prompt、Skill、Source、Tool Schema 与业务规则；工作台只消费 Runtime 的公开 API 和 AgentEvent。

## 6. 依赖边界

```text
apps/workbench ─┐
agents/* ───────┼──→ runtime public contracts
apps/api ───────┘             ↓
                         runtime internals

eval ──→ public API / public contracts
```

- `runtime/` 不得导入 `agents/`、`apps/` 或 `eval/`；
- `agents/` 通过公开 contracts 注册 Skill、Source、Tool 和业务 Policy；
- `apps/api/` 负责组装 Runtime、Provider Adapter、Store 和业务 Agent；
- `apps/workbench/` 不感知具体模型 SDK，不直接执行 Tool；
- `eval/` 只能通过 Runtime 的公开入口运行场景。

Runtime 核心不硬编码 `SKU`、`platform`、`price` 等领域字段。Workspace 可以保存品牌、渠道和店铺配置，但这些数据通过可扩展业务配置进入 Runtime，不成为 Runtime 内部条件分支。

## 7. Agent Runtime 核心执行链

### 7.1 Workspace / Session

- Workspace 管理品牌、渠道、店铺级长期配置、业务规则、可用 Agent 与 Sources；
- Session 隔离单次任务的上下文、ToolCalls、Permission Requests 和执行状态；
- 不同 Session 的消息、审批和工具结果严格隔离；
- Session 状态持久化，进程重启或任务中断后可以恢复。

### 7.2 Skill / Source

- Skill 描述业务 Agent 可完成的任务、使用规则和所需能力；
- Source 描述能力来源，包括本地实现、REST Adapter 和 MCP Server；
- Runtime 根据 Workspace、Agent 和权限配置装载当前 Session 可用的 Skill 与 Tool；
- Source 与 Skill 由 Runtime 统一管理，但其业务内容归属于上层 Agent。

### 7.3 AgentBackend

统一 `AgentBackend` 与 `ProviderAdapter` 屏蔽：

- 不同模型 SDK；
- 普通响应与流式协议；
- Tool Calling 格式；
- Token 与用量信息；
- Provider 错误格式、超时和取消。

Session、Skill、业务 Agent 和工作台不得依赖具体模型实现。首版提供一个真实 Provider Adapter 和一个确定性 Scripted Backend，后者用于测试与 Eval。

### 7.4 ToolCall / PreToolUse

ToolCall 在执行前依次经过：

```text
Schema Validation
    → Tool Resolution
    → Permission Mode
    → PreToolUse
    → Human Confirmation（按策略触发）
    → Tool Execution
    → ToolResult Normalization
    → AgentEvent Persistence
```

- Schema Validation 校验参数类型和必填字段；
- Permission Mode 决定当前工具允许、拒绝还是需要确认；
- PreToolUse 执行参数、作用域和副作用校验；
- 高风险操作进入 Permission Request，用户确认后从原 Session 继续；
- 隐藏 Tool 只用于减少误调用，不能替代执行前授权。

### 7.5 AgentEvent

Runtime 使用统一 AgentEvent 表达：

- Session 创建、运行、挂起、恢复、完成和失败；
- 模型响应与流式文本；
- ToolCall 发起、等待权限、执行和返回；
- Permission Request 创建、批准和拒绝；
- 异常、重试和恢复；
- 最终结果。

工作台、Trace、审计和 Eval 消费同一事件语义，但不能绕过 Runtime 改变执行状态。

## 8. 运行状态与恢复

- Runtime 在模型决策、权限挂起、工具执行前后和任务结束时保存关键状态；
- 恢复依据持久化 Session 和 ToolCall 状态，不依赖模型“记住”此前操作；
- 写操作使用稳定 `operation_id` 和幂等键，恢复时不得生成新的逻辑操作；
- 外部调用超时且结果不确定时进入 `UNKNOWN`，先回查再决定是否重试；
- 非法状态迁移被拒绝，并生成可审计事件。

这些能力用于实现图片所描述的“运行状态管理”和“任务中断后的会话恢复”，不是独立的平台产品。

## 9. Agent 工作台

### 9.1 Workspace 视图

- 创建、选择和切换 Workspace；
- 展示品牌、渠道、店铺及可用业务 Agent；
- 展示当前 Workspace 已启用的模型、Skills、Sources 和 Permission Mode；
- 业务配置通过扩展 Schema 渲染，工作台不硬编码商品领域流程。

### 9.2 Session 视图

- 创建、切换和恢复 Session；
- 以对话形式提交任务并展示流式响应；
- 展示 ToolCall、ToolResult 和当前执行状态；
- 展示 Permission Request，支持批准或拒绝；
- 支持中断、取消和恢复；
- 展示异常原因和最终结果。

### 9.3 前端边界

- 工作台只调用 Runtime API 并订阅 AgentEvent；
- 工作台不直接调用模型 SDK、MCP Server 或业务 REST API；
- 工作台不承担业务授权判断，只呈现 Runtime 的 PolicyDecision；
- 首版不建设低代码 Agent 编辑器和插件市场。

## 10. 商品运营 Agent

商品运营 Agent 是首个实际运行在 Runtime 上的业务 Agent。首版业务闭环包括：

1. 根据商品名称、SKU 或平台商品 ID 查询商品；
2. 获取商品在淘宝和抖音渠道的当前信息；
3. 生成商品维护计划；
4. 对敏感写操作发起 Permission Request；
5. 执行获批的价格修改；
6. 写后回查并返回真实执行状态；
7. 任务中断后从 Session 恢复。

商品 Tool、平台 Adapter、价格规则和领域 Policy 位于 `agents/product_operations/`。Runtime 不包含商品专用判断。

首版可以使用本地或模拟平台 Adapter，但必须明确标注；真实平台接入不是证明 Runtime 架构成立的前置条件。

## 11. Eval

### 11.1 Runtime Eval

验证 Runtime 的通用执行合同：

- Provider 输出是否正确标准化；
- Session 是否隔离并可恢复；
- Tool 参数和权限是否在执行前校验；
- Permission Request 是否从正确状态继续；
- 重试是否复用幂等键；
- UNKNOWN 是否先回查；
- AgentEvent 是否完整且顺序合法。

### 11.2 商品运营 Agent Eval

验证上层业务 Agent：

- 是否达到权威业务终态；
- 信息不足时是否停止危险写入；
- 业务规则是否阻止违规改价；
- 获批写操作是否完成并回查一致；
- 合法任务是否被错误拒绝。

两类 Eval 分别报告。Eval 结果是项目能力的验证证据，不是工作台的产品功能，也不包装为真实生产业务指标。

## 12. 目标目录结构

```text
runtime/
    contracts/          # 公共协议和数据结构
    orchestration/      # Agent Harness、Context 与 Agent Loop
    backend/            # AgentBackend 与 ProviderAdapter
    workspace/          # Workspace 配置与生命周期
    session/            # Session 状态、持久化与恢复
    capabilities/       # Skill、Source 与 Tool 装载
    execution/          # ToolCall 与 ToolResult
    permission/         # Permission Mode、PreToolUse 与人工确认
    events/             # AgentEvent、Trace 与审计

agents/
    product_operations/ # 商品运营 Agent

adapters/
    providers/          # 模型 Provider Adapter
    commerce/           # 淘宝、抖音或模拟平台 Adapter
    mcp/                # MCP Source Adapter

apps/
    api/                # FastAPI Runtime Host
    workbench/          # React + TypeScript Agent 工作台

eval/
    runtime/            # Runtime 行为合同评测
    product_operations/ # 商品运营任务评测

tests/
    runtime/
    agents/
    architecture/

docs/
    需求/
    计划/
    架构/
    评测/
```

## 13. 技术选型

- Runtime：Python 3.12、Pydantic v2、asyncio；
- Runtime Host：FastAPI、WebSocket；
- Agent 工作台：React、TypeScript；
- 能力协议：MCP、REST、本地 Python Tool；
- 状态存储：首版使用 SQLite，通过 Store Protocol 隔离实现；
- 测试：pytest；
- Eval：独立 Scenario、Runner、Scorer 和 Report。

技术栈可以与图片不同；项目对齐的是 Runtime 职责、执行链和产品结构，而不是复制 Electron、Bun 或具体模型 SDK。

## 14. 验收标准

1. 工作台可以完成 Workspace 选择、Session 对话、权限确认和中断恢复；
2. `Workspace / Session → Skill / Source → AgentBackend → ToolCall → PreToolUse → AgentEvent` 执行链完整可观测；
3. 切换 Provider 不需要修改 Session、Skill、业务 Agent 和工作台代码；
4. Runtime 不导入商品运营 Agent，商品 Agent 只通过公开 contracts 接入；
5. 不同 Session 的上下文、ToolCalls、Permission Requests 和执行状态严格隔离；
6. 本地 Tool、REST Adapter 和 MCP Server Tool 可以通过统一 Source 接口装载；
7. 高风险写操作未经授权不能到达 Tool 实现；
8. 中断任务可以从持久化状态恢复，且不会重复产生已提交副作用；
9. Runtime Eval 与商品运营 Agent Eval 可以独立运行和报告；
10. 项目文档和简历只声明代码、测试和 Eval 已验证的能力。

## 15. 首版完成定义

首版完成时，用户能够在 Agent 工作台中选择 Workspace，创建商品运营 Session，以对话方式发起商品查询和调价任务，查看模型输出与工具执行进度，对高风险操作进行确认，并在任务中断后恢复执行。底层 Runtime 能通过统一 AgentBackend、Skill/Source、ToolCall、PreToolUse 和 AgentEvent 支撑完整流程，并由独立 Eval 验证关键行为。
