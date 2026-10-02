# Agent Runtime 需求文档

> 版本：v0.1  
> 日期：2026-10-02  
> 状态：待评审

## 1. 项目定位

本项目从零设计并实现一个面向业务 Agent 的可扩展 Agent Runtime，为上层 Agent 提供统一的模型接入、上下文装配、能力加载、工具执行、权限治理、会话状态、故障恢复与事件审计能力。

项目主体是 Runtime，而不是某个垂直业务系统。电商商品维护 Agent 仅作为参考实现，用于验证 Runtime 能否承载具有查询、计划、审批、写入、回查和恢复等特征的真实业务流程。

项目形态为“Agent Runtime 系统 + Runtime Host/API + Runtime Console + 参考 Agent + Eval”。Runtime 是项目主体；Host/API 负责承载和暴露 Runtime；Runtime Console 提供会话操作、审批、恢复和执行观测入口。它们共同构成完整运行系统，但不扩展为多租户 SaaS 平台，也不以接入真实企业账号或产生线上业务指标为目标。

## 2. 要解决的问题

业务 Agent 通常会重复实现模型适配、Agent Loop、工具注册、上下文管理、权限校验、状态持久化和异常恢复，导致业务逻辑与执行基础设施耦合，难以复用、测试和审计。

本项目将这些共性能力下沉到 Runtime，使业务 Agent 只负责：

- 声明自身的 Prompt、Skill、Source 和 Tool；
- 定义领域参数 Schema 与业务规则；
- 实现或接入领域工具；
- 配置需要的权限、审批和恢复策略。

## 3. 术语边界

### 3.1 Agent Harness

Agent Harness 是 Runtime 内部面向模型的编排子系统，负责 Prompt 与 Context 装配、工具暴露、模型调用、Tool Result 回填、Agent Loop、轮次控制与上下文压缩。

### 3.2 Agent Runtime

Agent Runtime 是完整的生产执行底座。除 Agent Harness 外，还负责 Workspace、Session、状态持久化、权限审批、工具执行、幂等、超时重试、中断恢复、Trace 与审计。

### 3.3 Eval

Eval 是 Runtime 外部的评测系统，用例驱动 Runtime 或参考 Agent，并对结果、轨迹、安全边界和恢复行为进行判分。Eval 不属于 Agent Harness，也不参与生产执行。

### 3.4 Reference Agent

Reference Agent 是 Runtime 的业务接入示例。首个参考实现为电商商品维护 Agent，但 Runtime 核心不得包含 `SKU`、`platform`、`price`、`product` 等电商领域概念。

## 4. 项目目标

### 4.1 核心目标

1. 提供稳定、可测试的 Agent 执行生命周期。
2. 通过统一 `AgentBackend` 屏蔽模型厂商、流式协议和 Tool Calling 差异。
3. 通过 Workspace 和 Session 隔离长期配置与单次任务状态。
4. 通过 Skill、Source 和 Tool Registry 实现业务能力声明与加载。
5. 在工具执行前完成参数校验、权限判定、审批和通用执行策略校验。
6. 对写操作提供幂等、超时、分类重试、Checkpoint、中断恢复和未知结果处理能力。
7. 通过统一 AgentEvent 支撑 Runtime Console、Trace、审计和 Eval。
8. 通过独立 Eval 验证 Runtime contract 与参考 Agent 业务结果。

### 4.2 非目标

- 不建设多租户计费、组织管理、应用市场和云端部署控制面。
- 不承诺真实电商平台接入、生产 SLA 或线上业务价值指标。
- 不把工作流引擎、向量数据库、长期记忆和多 Agent 协作设为首版必选能力。
- 不通过堆叠基础设施证明“生产级”，只实现对执行正确性有直接价值的能力。

## 5. 架构原则

### 5.1 依赖方向

```text
apps / reference_agents / eval
              ↓
         runtime contracts
              ↓
        runtime internals
```

- `runtime/` 不得导入 `reference_agents/`、`apps/` 或 `eval/`。
- `reference_agents/` 只能依赖 Runtime 的公开 contracts，不依赖 Runtime 内部实现。
- `apps/` 负责组装 Runtime、Provider Adapter、Store 和 Reference Agent。
- `eval/` 只能从外部调用公开 API 或公开 Python 接口。

### 5.2 Runtime 不感知业务字段

Runtime 只处理通用对象：

- `AgentDefinition`
- `Workspace`
- `Session`
- `ModelRequest` / `ModelResponse`
- `ToolDefinition` / `ToolCall` / `ToolResult`
- `PolicyDecision` / `ApprovalRequest`
- `Checkpoint`
- `AgentEvent`

领域字段只允许出现在 Tool Schema、业务 Tool 实现和业务 Policy 中。

### 5.3 默认安全失败

缺少必填参数、权限不明确、审批未完成、工具结果未知或恢复状态不一致时，Runtime 必须停止推进对应写操作，并返回结构化状态；不得依赖模型自行判断是否继续。

## 6. 目标目录结构

```text
runtime/
    contracts/          # 公共协议与领域无关数据结构
    harness/            # Prompt、Context、Agent Loop、结果回填与压缩
    backend/            # AgentBackend 与 Provider Adapter 协议
    context/            # Workspace、Session 与 Context 装配
    capabilities/       # Skill、Source、Tool 注册和加载
    execution/          # ToolCall 生命周期与 Tool Executor
    policy/             # Permission、Approval 与 PreToolUse
    state/              # Store、Checkpoint、恢复与幂等记录
    observability/      # AgentEvent、Trace 与 Audit

reference_agents/
    ecommerce/          # 电商商品维护参考 Agent

apps/
    api/                # FastAPI 组装入口
    web/                # React + TypeScript Runtime Console

eval/
    runtime_contracts/  # Runtime 通用行为评测
    reference_agents/   # 参考 Agent 业务场景评测

tests/
    runtime/            # Runtime 单元与集成测试
    reference_agents/   # 参考 Agent 测试
    architecture/       # 依赖边界测试

docs/
    需求/
    计划/
    架构/
    评测/
```

## 7. 核心模块需求

### 7.1 Contracts

Contracts 是 Runtime 唯一稳定的公开边界，负责定义不可依赖具体 Provider、存储实现或业务领域的数据结构和协议。内部模块可以演进，但对外协议变更必须有版本说明和兼容策略。

### 7.2 Agent Harness

Agent Harness 必须支持：

- 根据 `AgentDefinition` 组装系统提示词、Skill、Source 和当前 Session Context；
- 向模型暴露当前 Session 有权使用的工具 Schema；
- 驱动模型响应、ToolCall、ToolResult 回填和下一轮决策；
- 限制最大轮次、上下文预算和单轮工具调用数；
- 在上下文压缩后保留任务目标、审批状态、未完成操作和关键工具结果；
- 生成完整 AgentEvent，而不是直接操作 Runtime Console 或 Eval。

### 7.3 AgentBackend

`AgentBackend` 必须统一：

- 普通与流式模型响应；
- Tool Calling 表达；
- Token 与用量信息；
- Provider 错误分类；
- 可重试与不可重试错误；
- 模型取消和超时。

首版至少提供一个真实 Provider Adapter 和一个确定性 Scripted Backend，后者用于测试与 Eval。

### 7.4 Workspace 与 Session

- Workspace 保存 Agent Definition、可用能力、长期配置和默认 Policy。
- Session 保存单次任务的消息、ToolCall、ApprovalRequest、Checkpoint 和执行状态。
- 不同 Session 的消息、审批和工具结果必须严格隔离。
- Session 可从持久化状态恢复，不依赖进程内对象继续存在。

### 7.5 Capability System

- Skill 描述业务能力、使用规则和可选资源。
- Source 提供 REST、MCP、本地实现或其他能力来源。
- Tool Registry 将 Source 暴露的能力标准化为 `ToolDefinition`。
- Runtime 根据 Agent Definition、Workspace 配置和当前权限动态计算可见工具。
- 工具隐藏只用于减少模型误调用，真正授权必须由执行前 Policy 再次校验。

### 7.6 Tool Execution 与 Policy

一次 ToolCall 必须依次经过：

```text
Schema Validation
    → Capability Resolution
    → Permission Check
    → Approval Check
    → PreToolUse Policy
    → Idempotency Reservation
    → Tool Execution
    → Result Normalization
    → Event / Audit Persistence
```

任一步失败都要产生结构化 `ToolResult` 或挂起状态。业务 Policy 可以读取 Tool 名称、参数、用户上下文和领域证据，但不能绕过 Runtime 的通用执行状态机。

### 7.7 State、Checkpoint 与 Recovery

- Runtime 在模型决策、审批挂起、工具执行前后和最终结束时持久化关键状态。
- 写操作使用稳定 `operation_id` 与幂等键，恢复时不得更换。
- 超时后无法确认外部结果时进入 `UNKNOWN`，先回查再决定是否重试。
- 恢复逻辑基于持久化事实，而不是让模型回忆之前发生的动作。
- 状态迁移必须可审计，并拒绝非法迁移。

### 7.8 Observability

统一 AgentEvent 至少覆盖：

- Session 生命周期；
- 模型请求与响应摘要；
- ToolCall 发起、挂起、执行和完成；
- 权限拒绝与审批；
- Checkpoint 与恢复；
- 最终结果与异常。

AgentEvent 同时服务于 Runtime Console、Trace、审计和 Eval，但不同消费者不能绕过 Runtime API 反向影响执行。

### 7.9 Runtime Console

Runtime Console 是 Runtime 的运行控制面，不承载电商等领域业务逻辑。首版必须支持：

- 创建 Workspace 和 Session，并查看当前运行状态；
- 实时展示模型轮次、ToolCall、ToolResult 和 AgentEvent；
- 展示待审批请求，并通过 Runtime API 提交批准或拒绝；
- 发起中断、恢复和取消操作；
- 查看 Checkpoint、Trace、审计记录与最终结果；
- 根据通用 Tool Schema 和 Event Schema 渲染信息，不直接依赖参考 Agent 的内部实现。

## 8. 参考 Agent

首个参考 Agent 为电商商品维护 Agent，用于验证：

- 多轮参数收集与业务对象定位；
- 查询、计划、审批、写入、回查和恢复的完整流程；
- 枚举型业务范围和写操作安全；
- REST、MCP 或本地 Source 的统一接入方式。

电商 Agent 自行实现商品 Tool、平台 Adapter、价格规则和业务 Policy。Runtime 不提供任何电商专用分支。

首版可以使用本地或模拟平台 Adapter，但必须明确标注，不将模拟结果描述为真实平台生产指标。

## 9. Eval 设计

### 9.1 Runtime Contract Eval

评测 Runtime 是否满足通用执行合同：

- Provider 输出是否被正确标准化；
- Tool Schema 与参数校验是否可靠；
- 未授权写操作是否在执行前被拦截；
- Session 是否隔离；
- 审批后是否从正确 Checkpoint 恢复；
- 重试是否复用幂等键；
- 未知结果是否先回查；
- AgentEvent 是否完整且顺序合法。

### 9.2 Reference Agent Eval

评测电商参考 Agent 的业务能力：

- 任务是否达到权威业务终态；
- 必要信息不足时是否停止写入并请求补充；
- 业务规则是否阻止危险写操作；
- 执行后回查结果是否一致；
- 合法任务是否被错误拒绝。

两类 Eval 分别报告，不将业务任务成功率包装为 Runtime 的整体正确率。

## 10. 技术选型

- Runtime：Python 3.12、Pydantic v2、asyncio；
- API：FastAPI、WebSocket；
- Runtime Console：React、TypeScript；
- 能力协议：MCP、REST、本地 Python Tool；
- 首版持久化：SQLite 或文件型实现，通过 Store Protocol 隔离；
- 测试：pytest；
- Eval：独立场景数据、Runner、Scorer 与报告模块。

## 11. 重写与迁移策略

1. 在新仓库中从 Contracts 和架构边界测试开始，不复制旧仓库目录结构。
2. 实现最小 Runtime 闭环：Scripted Backend 调用一个通用 Tool，并持久化 Session 和 AgentEvent。
3. 补齐权限、审批、Checkpoint、幂等和恢复。
4. 接入真实 Provider Adapter、Runtime Host/API 与 Runtime Console。
5. 将旧项目中的电商能力按新 contracts 重新实现为 Reference Agent，不直接迁移领域耦合的执行层。
6. 建立 Runtime Contract Eval 和 Reference Agent Eval。
7. 新 Runtime 达到验收标准后，旧仓库只作为历史参考，不参与新项目构建和运行。

## 12. 验收标准

1. `runtime/` 对 `reference_agents/`、`apps/` 和 `eval/` 零反向依赖，并由自动化架构测试保证。
2. Runtime 核心源码和核心测试不出现电商领域概念。
3. 移除电商参考 Agent 后，Runtime 的单元、集成和 Contract Eval 仍可独立运行。
4. 一个 Agent 可以仅通过公开 contracts 注册 Backend、Skill、Source、Tool 和 Policy 后运行。
5. Session 重启恢复不依赖进程内状态，审批和未完成操作不会丢失。
6. 写操作在无权限、未审批、参数不完整或结果未知时不会产生未经验证的重复副作用。
7. Runtime、Reference Agent 和 Eval 的文档、目录和报告使用统一术语：Harness 是 Runtime 内部模型编排子系统，Eval 是外部评测系统。
8. 所有关键状态迁移都能通过 AgentEvent 与审计记录还原。
9. 项目文档只声明由代码、测试和 Eval 结果支持的能力，不声称真实企业生产部署。

## 13. 首版完成定义

首版完成时，应具备一个边界独立且可运行的 `runtime` Python 核心包、一个承载 Runtime 的 API Host、一个用于会话操作与执行观测的 Runtime Console、一个电商商品维护参考 Agent、独立的 Runtime Contract Eval 与业务 Eval，以及覆盖权限、幂等、恢复和架构边界的自动化测试。`runtime` 包是项目内部的核心代码边界，不以对外发布 SDK 为首版目标。
