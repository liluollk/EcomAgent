# Agent 工作台与 Runtime 需求文档

> 版本：v0.4
> 日期：2026-10-02
> 状态：已确认

## 1. 项目定位

本项目面向企业内部业务场景，从零设计并实现一套 Agent 工作台及其通用 Agent Runtime。Agent Runtime 是核心技术主体，统一模型接入、能力装载、Workspace/Session/Run 状态、工具权限、持久执行和故障恢复；Agent 工作台是产品载体，为业务人员提供任务对话、执行进度、信息补充、写前确认和中断恢复入口。

商品运营 Agent 是首个业务参考实现，覆盖商品查询、维护计划、敏感写操作确认、执行和结果回查，用于验证 Runtime 能支撑具有真实业务约束和写操作风险的任务闭环。`SKU`、平台、价格等电商概念只存在于商品运营 Agent，Runtime 不依赖具体业务领域。

项目不是单一的“商品调价 Agent”，也不定位为包含计费、应用市场和云端控制面的商业化 Agent 平台。最终交付物由三层组成：

```text
Agent Runtime          核心技术主体
    ↓
Agent 工作台           产品载体
    ↓
商品运营 Agent         业务参考实现
```

FastAPI、React、Mem0、WeKnora 和 Langfuse 等属于实现选型或外部集成，不作为项目定位。Eval 属于工程验证体系，不作为面向用户的产品模块。

## 2. 用户与价值

### 2.1 目标用户

工作台面向企业内部业务人员；首个参考场景的直接用户是需要跨淘宝、抖音店铺处理商品维护任务的运营成员。首版不另外定义 Agent 管理者、研发人员或审计人员等独立产品角色，Workspace 配置由具备相应权限的内部成员完成。

### 2.2 核心价值

- 业务人员通过统一工作台使用 Agent，不需要理解模型和工具接入细节；
- 新业务 Agent 复用 Runtime 的模型、会话、能力、权限和状态管理，不重复建设执行基础设施；
- 高风险工具调用在执行前经过代码侧校验和人工确认，避免仅依赖模型自律；
- 任务中断后可以从持久化 Session 与 Run 恢复，不需要重新开始整段对话和操作。

## 3. 项目边界

### 3.1 首版范围

- Agent Runtime：作为核心技术主体，实现完整执行链、模型抽象、能力装载、权限治理、持久状态和故障恢复；
- Agent 工作台：作为产品载体，提供 Workspace 选择、Session 对话、执行状态、信息补充、写前确认和恢复入口；
- 商品运营 Agent：作为首个业务参考实现，验证 Runtime 与业务代码的解耦边界及完整任务闭环；
- 能力接入：本地 Tool 与 MCP Server；淘宝、抖音等业务 API 通过商品运营 Agent 内部的代码 Adapter 接入；
- 长期记忆、知识库、Trace 和 Eval 分别通过 Mem0、WeKnora、Langfuse 与独立 Eval 模块实现；
- Eval：分别验证 Runtime 行为合同和商品运营任务结果。

### 3.2 首版不做

- 多租户计费、组织管理、应用市场和云端部署控制面；
- 真实企业账号体系和生产 SLA；
- 同时实现店铺运营、活动运营和客服售后三套业务 Agent；
- Redis、独立工作流引擎、多 Agent 协作和通用低代码 OpenAPI-to-Tool 平台；
- 把任意 REST API 在前端动态转换为 Tool；业务 API 由对应业务 Agent 的代码 Adapter 显式实现；
- 将模拟平台结果描述为真实线上业务指标。

## 4. 术语边界

### 4.1 Agent Runtime

Agent Runtime 是业务 Agent 的统一运行底座，负责 Workspace、Session、能力装载、模型调用、工具执行、权限等待、异常处理、状态持久化和 AgentEvent。

### 4.2 Agent Harness

Agent Harness 是 Runtime 内部面向模型的编排部分，负责 Prompt/Context 装配、工具暴露、Agent Loop、Tool Result 回填和上下文压缩。Harness 不是 Eval，也不是整个 Runtime。

### 4.3 Agent 工作台

Agent 工作台是运营人员使用业务 Agent 的产品界面，负责 Workspace 与 Session 导航、任务对话、工具进度、Input Request、Confirmation Request、执行状态和恢复操作。工作台通过 Runtime API 和 AgentEvent 工作，不直接调用模型 SDK 或业务工具。

### 4.4 Eval

Eval 是 Runtime 外部的工程验证系统，通过固定场景、Runner 和 Scorer 检查执行结果与轨迹。Eval 不参与生产执行，不称为 Harness。

## 5. 总体架构

```text
Agent 工作台
    ├── Workspace：品牌 / 渠道 / 店铺级配置
    ├── Session：多轮对话与上下文
    ├── Run：ToolCall / Input Request / Confirmation Request / 执行状态
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
    Model Provider / Local Tool / MCP Server
    Mem0 / WeKnora / Langfuse / PostgreSQL
```

Runtime 抽象模型调用、工具执行、权限等待和异常处理；商品运营 Agent 只定义 Prompt、Skill、Source、Tool Schema 与业务规则；工作台只消费 Runtime 的公开 API 和 AgentEvent。

## 6. 依赖边界

```text
apps/workbench ─┐
agents/* ───────┼──→ Runtime 公开接口与协议
apps/api ───────┘             ↓
                         runtime internals

eval ──→ 公开 API / 公开接口与协议
```

- `runtime/` 不得导入 `agents/`、`apps/` 或 `eval/`；
- `agents/` 通过 Runtime 公开接口与协议注册 Skill、Source、Tool 和业务 Policy；
- `apps/api/` 负责身份校验、配置管理、命令接收和查询，不直接运行长时 Agent Loop；
- `apps/worker/` 负责领取持久化 Run Job，组装 Runtime、AgentBackend、Store 和业务 Agent，并推进执行；
- `apps/workbench/` 不感知具体模型 SDK，不直接执行 Tool；
- `eval/` 只能通过 Runtime 的公开入口运行场景。

Runtime 核心不硬编码 `SKU`、`platform`、`price` 等领域字段。Workspace 可以保存品牌、渠道和店铺配置，但这些数据通过可扩展业务配置进入 Runtime，不成为 Runtime 内部条件分支。

## 7. Agent Runtime 核心执行链

### 7.1 Workspace / Session

- Workspace 对应一个品牌或业务单元，管理渠道、多个淘宝/抖音店铺、业务规则、可用 Agent 与 Sources；
- Session 管理一次持续对话的消息与上下文，可以包含多个 Run；
- Run 表示由一条用户消息或一次恢复操作触发的执行过程，隔离 ToolCalls、Input Requests、Confirmation Requests 和执行状态；
- 同一 Session 同一时刻最多存在一个活动 Run，避免并发执行污染上下文或重复产生副作用；
- 不同 Session 的消息、确认和工具结果严格隔离；
- Session 与 Run 状态持久化，进程重启或任务中断后可以恢复。

### 7.2 Skill / Source

- Skill 描述业务 Agent 可完成的任务、使用规则和所需能力；
- Source 描述能力来源，包括本地实现和 MCP Server；
- Runtime 根据 Workspace、Agent 和权限配置装载当前 Session 可用的 Skill 与 Tool；
- Source 与 Skill 由 Runtime 统一管理，但其业务内容归属于上层 Agent。

淘宝、抖音等业务 REST API 不作为通用 Source 动态导入。商品运营 Agent 通过明确的代码 Adapter 调用平台 API，再以业务 Tool 暴露给 Runtime。WeKnora 作为知识服务通过 MCP Source 接入，Runtime 不依赖其内部检索实现。

### 7.3 AgentBackend

统一 `AgentBackend` 屏蔽：

- 不同模型 SDK；
- 普通响应与流式协议；
- Tool Calling 格式；
- Token 与用量信息；
- Provider 错误格式、超时和取消。

生产实现只保留两类协议后端：`OpenAIBackend` 和 `AnthropicBackend`。DeepSeek 等 OpenAI 兼容服务使用 `OpenAIBackend`，不按模型厂商新增子类。`ScriptedBackend` 仅用于测试与 Eval，不属于第三种生产协议。前端配置协议类型、Base URL、API Key 和 Model；API Key 必须加密保存且读取时脱敏。

Session、Skill、业务 Agent 和工作台不得依赖具体模型 SDK 或厂商实现。

### 7.4 ToolCall / PreToolUse

ToolCall 在执行前依次经过：

```text
Schema Validation
    → Tool Resolution
    → Business Input Validation
    → Access Control
    → Business Policy
    → Execution Mode
    → Write Confirmation（按策略触发）
    → Tool Execution
    → ToolResult Normalization
    → AgentEvent Persistence
```

Runtime 将 Tool Resolution 之后、Tool Execution 之前的校验与决策步骤统一纳入 PreToolUse 管线，但每项规则仍由所属层实现，不能把业务判断堆入 Runtime 核心。

- Schema Validation 校验参数类型和必填字段；
- 商品运营 Agent 通过 Runtime 公开接口注册业务输入校验器，识别店铺、平台等执行所需信息是否缺失、无效或来源不可信；
- Runtime 只负责调用校验器并处理其结构化结果，不硬编码 `store_id`、`platform`、`SKU` 等商品领域规则；
- Access Control 校验成员身份、Workspace 与店铺数据范围和动作权限；
- Business Policy 校验价格、活动状态等业务约束，规则实现属于商品运营 Agent；
- Execution Mode 决定写操作被拒绝、进入写前确认，还是允许自动执行；
- 写操作需要确认时进入 Confirmation Request，用户确认后从原 Run 继续；
- 隐藏 Tool 只用于减少误调用，不能替代执行前授权。

成员身份与执行模式是两条独立边界：

- RBAC 在 Workspace 级管理成员角色，在店铺级限制数据范围，并通过动作权限控制是否可以查询或修改；首版不实现 SKU 级授权；
- `READ_ONLY` 禁止所有写 Tool，`CONFIRM_WRITES` 要求当前操作者在每次写入前确认，`AUTO_EXECUTE` 允许符合权限和业务规则的写操作直接执行；
- Confirmation Request 是当前操作者的写前确认，不是另一名成员的审批流；即使处于 `AUTO_EXECUTE`，Access Control 和 Business Policy 仍必须通过。

### 7.5 Input Request / Confirmation Request

模型可见工具只包含业务 Tool，不提供 `request_user_input`。人机交互分为两类：

- 模型在形成 ToolCall 前发现任务信息不足时，可以直接用自然语言询问；本次 Run 正常结束，用户下一条消息启动后续 Run；
- ToolCall 已经形成，但 Schema Validation 或业务输入校验发现执行所需信息缺失、无效或来源不可信时，由业务 Agent 注册的输入要求解析器返回结构化 `InputRequirement`，Runtime 据此创建 Input Request，并挂起同一个 Run。通用 Schema 只报告字段问题，不负责生成商品领域问题和候选项。

各层职责如下：

- 模型负责提出行动及参数，不负责声明参数已经通过授权或业务校验；
- 业务 Agent 负责识别业务信息缺失并生成 `InputRequirement`，包括字段标识、问题、回答 Schema 和可选项；
- Runtime 负责持久化 Input Request、切换 Run 状态、发出事件、接收并校验回答以及恢复执行；
- 工作台负责渲染问题和候选项、收集当前用户回答，不自行修改 Run 或 ToolCall 状态。

候选店铺、平台等内容必须由业务层根据 Workspace 配置、成员数据范围和实时资源生成，不允许模型自由编造。只有具备当前 Session 访问权限的成员才能回答待处理请求；同一请求只能处理一次，过期、取消或已经处理的请求不得再次恢复 Run。用户回答按 Input Request 的 Schema 校验，并记录回答人、回答时间和关联的 ToolCall。

恢复后不得直接调用 Tool。Runtime 必须重新执行 Schema Validation、业务输入校验、Access Control、Business Policy 和 Execution Mode；若写操作需要确认，再进入 `WAITING_CONFIRMATION`。输入请求与写前确认是两种独立交互，不能共用一个模糊的 Permission Request。

### 7.6 AgentEvent

Runtime 使用统一 AgentEvent 表达：

- Session 与 Run 创建、运行、完成和失败；
- 模型响应与流式文本；
- ToolCall 发起、等待输入、等待确认、执行和返回；
- `INPUT_REQUIRED`、`INPUT_RECEIVED`、`CONFIRMATION_REQUIRED`、`CONFIRMATION_ACCEPTED`、`CONFIRMATION_REJECTED` 和 `RUN_RESUMED`；
- 异常、重试和恢复；
- 最终结果。

工作台、Trace、审计和 Eval 消费同一事件语义，但不能绕过 Runtime 改变执行状态。

### 7.7 Memory / Knowledge / Observability

- Session 消息和 Run 状态属于短期执行上下文，由 Runtime 存入 PostgreSQL；
- 长期记忆只保存已经确认的用户偏好、反馈、决策和经验，不保存权限、店铺作用域、价格规则、凭证或本次执行参数等关键事实；
- 应用数据库保存长期记忆的权威内容与同步状态，Mem0 OSS 负责提取和检索，`BAAI/bge-small-zh-v1.5` 作为本地 Embedding 模型，向量写入 pgvector；
- WeKnora 保存商品知识、品牌规范、运营说明和 SOP 文档，通过 MCP 暴露检索能力。知识库提供事实资料，Skill 定义完成任务的方法和约束，二者不能互相替代；
- Langfuse 记录模型、Tool 和 Run 的 Trace，并承载 Eval Experiment 与 Score；PostgreSQL 中的 Runtime 状态和审计记录才是业务权威数据，Langfuse 不参与状态恢复或授权判断。

## 8. 运行状态与恢复

- Run 使用 `RUNNING`、`WAITING_INPUT`、`WAITING_CONFIRMATION`、`COMPLETED`、`FAILED` 和 `CANCELLED` 等原因明确的持久状态；不使用无法表达等待原因的通用 `INTERRUPTED` 状态；
- `INPUT_REQUIRED` 和 `RUN_RESUMED` 是 AgentEvent，不是持久状态；`RESUMED` 只描述一次状态迁移；
- Runtime 在模型决策、输入挂起、确认挂起、工具执行前后和任务结束时保存关键状态；
- 恢复依据持久化 Session 和 ToolCall 状态，不依赖模型“记住”此前操作；
- Input Request 和 Confirmation Request 必须具有稳定 ID、关联 Run 与 ToolCall、请求状态、回答 Schema、创建与处理时间以及操作人；
- 写操作使用稳定 `operation_id` 和幂等键，恢复时不得生成新的逻辑操作；
- 外部调用超时且结果不确定时进入 `UNKNOWN`，先回查再决定是否重试；
- 非法状态迁移被拒绝，并生成可审计事件。

API 与 Worker 不依赖 Redis：待执行任务写入 PostgreSQL 的 Run Job 表，Worker 通过数据库租约领取任务。模型或 Tool 外部调用不得发生在长数据库事务内；租约、稳定 `operation_id`、幂等键和写后回查共同防止恢复时重复写入。

这些能力用于实现图片所描述的“运行状态管理”和“任务中断后的会话恢复”，不是独立的平台产品。

## 9. Agent 工作台

### 9.1 Workspace 视图

- 创建、选择和切换 Workspace；
- 展示品牌、渠道、店铺及可用业务 Agent；
- 展示当前 Workspace 已启用的模型、Skills、Sources 和 Execution Mode；
- 配置 Workspace 成员角色、店铺数据范围、动作权限和 Execution Mode；
- 配置模型连接、Skill、MCP Source、WeKnora 知识库连接和业务规则；
- 业务配置通过扩展 Schema 渲染，工作台不硬编码商品领域流程。

### 9.2 Session 视图

- 创建、切换和恢复 Session；
- 以对话形式提交任务并展示流式响应；
- 展示 ToolCall、ToolResult 和当前执行状态；
- 展示 Input Request 并按 Schema 收集回答；
- 展示 Confirmation Request，支持确认或拒绝写操作；
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
4. 缺少执行信息时发起 Input Request，并在用户补充后恢复原 Run；
5. 对敏感写操作发起 Confirmation Request；
6. 执行已确认的价格修改；
7. 写后回查并返回真实执行状态；
8. 任务中断后从 Session 恢复。

商品 Tool、平台 Adapter、价格规则和领域 Policy 位于 `agents/product_operations/`。Runtime 不包含商品专用判断。

首版可以使用本地或模拟平台 Adapter，但必须明确标注；真实平台接入不是证明 Runtime 架构成立的前置条件。

## 11. Eval

### 11.1 Runtime 核心行为评测

Runtime 核心行为评测使用 `ScriptedBackend`、确定性 Tool 和故障注入验证必须始终成立的执行规则，不依赖模型随机性。以下能力属于系统不变量，目标是全部通过：

- Provider 输出是否正确标准化；
- Session 是否隔离并可恢复；
- Local 与 MCP Tool 是否通过统一 Source 接口装载；
- Tool 参数、权限和副作用是否在执行前校验；
- Input Request 和 Confirmation Request 是否从正确状态继续，并在恢复后重新执行完整校验；
- 重试是否复用幂等键；
- UNKNOWN 是否先回查；
- AgentEvent 是否完整且顺序合法。

### 11.2 商品运营 Agent Eval

商品运营 Agent Eval 使用真实模型验证六类任务：只读查询、合法写操作、信息不完整、高风险请求、多轮任务和查询—计划—确认—执行—回查组合任务。

核心指标包括：

- **任务完成率**：仅统计允许执行的任务，以平台或模拟平台的权威最终状态判定，不以模型文字声明为准；
- **行为决策正确率**：当前状态下是否正确提出业务 ToolCall、用自然语言补充询问或拒绝请求；Input Request 与 Confirmation Request 的状态迁移由 Runtime 核心行为评测单独验证；
- **危险副作用率**：安全对抗场景中实际产生未授权或违规写入的比例，必须为 0；
- **合法任务误拒率**：合法任务被错误拒绝或无法推进的比例，用于防止系统通过一律拒绝获得安全高分。

缺字段后的询问属于行为决策正确率的一个切片，不单独包装为主要指标。

### 11.3 故障恢复 Eval

通过故障脚本验证模型超时、Tool 调用前中断、Tool 已成功但响应丢失、确认后重启、平台暂时不可用、回查暂时不一致和重复提交等场景。

报告以下指标：

- 恢复成功率；
- 重复副作用数；
- 最终状态一致率；
- UNKNOWN 状态处理正确率。

关键验收场景是：Tool 已经产生写入但 Runtime 未收到响应时，恢复后必须先回查，不能再次执行同一写操作。

### 11.4 工作台端到端验收

工作台使用固定流程和浏览器自动化验证，不计入模型任务完成率：

- 创建和切换 Workspace；
- 创建、切换和恢复 Session；
- 实时展示 ToolCall、ToolResult 和 AgentEvent；
- 展示并处理 Input Request 和 Confirmation Request；
- 补充信息或确认写操作后继续原 Run；
- 中断后恢复正确执行状态；
- 最终展示结果与 Runtime 权威状态一致。

### 11.5 场景数据与报告

每条模型评测场景必须声明：

- 初始业务状态；
- 用户输入与多轮消息；
- Workspace、Session、权限和可用能力配置；
- 故障注入条件；
- 期望最终状态；
- 允许和禁止产生的副作用；
- 允许的合法执行路径。

评测集按任务类型、信息完整度、权限状态、风险等级、单轮/多轮、正常/故障和普通/对抗表达组合覆盖，不要求穷举笛卡尔积。

数据分为可迭代的开发集、固化历史失败的回归集和不根据单条失败修改规则的冻结测试集。最终报告分别展示 Runtime 核心行为、任务完成率、行为决策正确率、危险副作用率、合法任务误拒率和故障恢复成功率，不生成掩盖问题来源的综合总分。

Eval 结果是项目能力的验证证据，不是工作台的产品功能，也不包装为真实生产业务指标。

## 12. 目标目录结构

```text
runtime/
    interfaces/         # 公共接口和数据结构
    orchestration/      # Agent Harness、Context 与 Agent Loop
    backend/            # AgentBackend 公共接口
    workspace/          # Workspace 配置与生命周期
    session/            # Session 状态、持久化与恢复
    capabilities/       # Skill、Source 与 Tool 装载
    execution/          # ToolCall 与 ToolResult
    access/             # 成员权限、数据范围与动作授权
    interaction/        # Input Request、Confirmation Request 与恢复
    memory/             # 长期记忆接口与同步流程
    events/             # AgentEvent 与审计

agents/
    product_operations/ # 商品运营 Agent

adapters/
    model_backends/     # OpenAIBackend 与 AnthropicBackend
    commerce/           # 淘宝、抖音或模拟平台 Adapter
    mcp/                # MCP Source Adapter
    mem0/               # Mem0 长期记忆集成
    langfuse/           # Trace 与 Eval 集成

apps/
    api/                # FastAPI 命令、查询与事件接口
    worker/             # PostgreSQL Job Worker 与 Runtime Host
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
- Runtime Host：FastAPI、WebSocket、独立 Runtime Worker；
- Agent 工作台：React、TypeScript；
- 能力协议：MCP、本地 Python Tool；业务平台使用代码 Adapter；
- 状态存储：PostgreSQL、SQLAlchemy 2、Alembic；
- 长期记忆：Mem0 OSS、pgvector、`BAAI/bge-small-zh-v1.5`；
- 知识库：WeKnora，通过 MCP 接入；
- 可观测与实验：Langfuse；
- 任务领取：PostgreSQL Run Job 与租约，不引入 Redis；
- 测试：pytest；
- Eval：独立 Scenario、Runner、Scorer 和 Report。

技术栈可以与图片不同；项目对齐的是 Runtime 职责、执行链和产品结构，而不是复制 Electron、Bun 或具体模型 SDK。

## 14. 验收标准

1. 工作台可以完成 Workspace 选择、Session 对话、信息补充、写前确认和中断恢复；
2. `Workspace / Session → Skill / Source → AgentBackend → ToolCall → PreToolUse → AgentEvent` 执行链完整可观测；
3. 在 `OpenAIBackend` 与 `AnthropicBackend` 之间切换不需要修改 Session、Skill、业务 Agent 和工作台代码；
4. Runtime 不导入商品运营 Agent，商品 Agent 只通过公开接口与协议接入；
5. 不同 Session 的上下文严格隔离，每个 Run 的 ToolCalls、Input Requests、Confirmation Requests 和执行状态均可独立追踪；
6. 本地 Tool 和 MCP Server Tool 可以通过统一 Source 接口装载，业务平台 API 通过商品运营 Agent 的代码 Adapter 接入；
7. 高风险写操作未经授权不能到达 Tool 实现；
8. 缺失或不可信的关键业务信息必须进入 `WAITING_INPUT`，不得由 Runtime 或模型猜测后执行；
9. 用户补充信息后必须重新执行完整校验，不能从 `WAITING_INPUT` 直接进入 Tool 执行；
10. 中断任务可以从持久化状态恢复，且不会重复产生已提交副作用；
11. Runtime Eval 与商品运营 Agent Eval 可以独立运行和报告；
12. API 进程重启不丢失待执行 Run，Worker 可通过 PostgreSQL 租约继续领取任务；
13. Mem0、WeKnora 或 Langfuse 单独不可用时，不得破坏 Runtime 权威状态和安全边界；
14. 项目文档和简历只声明代码、测试和 Eval 已验证的能力。

## 15. 首版完成定义

首版完成时，项目应形成一套可独立复用的 Agent Runtime、一个通过公开 API 使用 Runtime 的 Agent 工作台，以及一个不侵入 Runtime 核心的商品运营参考 Agent。

用户能够在工作台中选择 Workspace，创建商品运营 Session，以对话方式发起商品查询和调价任务，查看模型输出与工具执行进度，补充执行所需信息，对高风险写操作进行确认，并在任务中断后恢复执行。Runtime 通过统一 AgentBackend、Skill/Source、ToolCall、PreToolUse、Input Request、Confirmation Request 和 AgentEvent 支撑完整流程；商品运营 Agent 只通过 Runtime 公开接口与协议接入；独立 Eval 分别验证 Runtime 不变量、商品运营任务结果和故障恢复行为。

该完成定义证明的是“通用 Runtime 能够承载一个具有真实约束的业务 Agent”，不等同于已经建设多种业务 Agent、接入真实生产流量或达到企业生产 SLA。
