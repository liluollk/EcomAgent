# 电商运营agent

**技术栈：** Python、FastAPI、TypeScript、React、PostgreSQL

面向电商企业内部商品运营场景，设计并实现电商运营 Agent 工作台，支持运营成员以对话方式处理多品牌、多店铺的商品维护任务，围绕跨渠道商品查询、价差识别、受控改价与结果回查形成完整闭环。

- **Agent Runtime 核心架构：** 围绕 Workspace / Session 构建由 Skill / Source、AgentBackend、ToolCall、PreToolUse 与 AgentEvent 组成的统一执行链路，将模型调用、能力装载、工具执行、权限控制及异常恢复与商品运营逻辑解耦。

- **Tool 权限与安全执行：** 针对商品调价等高风险写操作，设计 RBAC、店铺级资源授权与只读、写前确认、自动执行三种模式；由 PreToolUse 统一校验成员权限、目标店铺和业务规则，并按执行模式阻断、等待确认或放行，在模型决策与真实副作用之间建立确定性安全边界。

- **Agent 能力装载：** 针对商品平台、知识库及本地能力接入方式不一致的问题，设计 Source / Skill 分层的能力装载机制，基于 Adapter 与 MCP 统一接入 REST API、Local Tool 和 MCP Server 工具。

- **Agent 评测：** 构建覆盖商品查询、改价、多店决策与多轮交互的评测集，以业务终态和工具调用轨迹判定执行结果；基于失败案例优化提示词、Skill 与工具参数定义，将多步骤任务完成率从 **50.0% 提升至 83.3%**，并验证越权写操作可在执行前被拦截。
