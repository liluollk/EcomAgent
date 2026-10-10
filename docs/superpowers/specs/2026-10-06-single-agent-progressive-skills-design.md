# 单业务 Agent 与 Skill 按需加载设计

## 1. 目标

将当前产品边界统一为“一个 Workspace 绑定一个商品运营 Agent”，用户只切换 Workspace，不在任务开始时选择业务 Agent；同时为商品运营 Agent 提供轻量的多 Skill 使用机制：模型常驻看到 Skill 名称和描述，认为相关时再加载完整指令，不增加独立 Router、分类模型或向量检索。

本次改动还会把商品运行组合根从 `adapters/commerce/product_runtime.py` 移到独立组合模块，恢复 `adapters` 只实现外部平台端口的职责。

## 2. 当前代码事实

- `runtime/` 不导入 `agents/`、`adapters/`、`apps/` 或 `eval/`，现有架构测试保护了依赖方向。
- `WorkspaceOverviewService` 只接受零个或一个有效 Agent 绑定，多个有效绑定返回配置错误。
- 工作台没有 Agent 选择器，但 `TaskComposer` 仍把 `overview.agent.id` 作为 `agent_id` 回传。
- `/api/agents` 返回全局 Agent 目录，与“Workspace 唯一绑定”产品语义重复。
- 当前 `ProductMaintenanceAgent` 只有一个 `product_maintenance` Skill，完整 Skill 指令作为受保护的 Agent 指令常驻上下文。
- `adapters/commerce/product_runtime.py` 同时负责 Agent、Skill、Tool、Policy、Backend 和 Runtime 的完整组装，超出了 Adapter 的职责。

## 3. 选定方案

### 3.1 Agent 选择

工作台不提供业务 Agent 选择器，也不让模型在多个业务 Agent 间自动路由。

创建 Session 时，客户端只提交 `workspace_id` 和可选标题。应用服务从当前 Workspace 解析唯一有效且已发布的 AgentVersion，并把 `agent_id` 与 `agent_version_id` 固定到 Session。零个绑定返回明确的配置错误；多个有效绑定继续视为配置不变量破坏。

Runtime 内部继续保留通用 Agent、AgentVersion、WorkspaceAgentBinding 和 AgentRegistry，因为它们用于版本快照和 Runtime 复用；收口的是产品入口，不是删除 Runtime 的通用抽象。

### 3.2 Skill 发现与加载

不实现独立 Skill Router。商品运营 Agent 的同一次模型推理负责判断任务是否需要某个 Skill。

上下文分为两部分：

1. 常驻 Skill 目录：仅包含 `stable_key`、名称、简短描述和版本；
2. 已激活 Skill 指令：只包含本次 Run 已加载 Skill 的完整说明。

Runtime 提供内置只读控制能力 `load_skill`。模型根据 Skill 描述选择 `skill_key` 后调用它；Runtime 只允许加载当前 Run 已固定版本集合中的 Skill，成功后记录激活事实，并在下一次模型调用时把完整 Skill 指令加入受保护上下文。

`load_skill` 不执行业务操作，不经过业务写确认，但必须进行输入 Schema 校验、可见范围校验和幂等处理。重复加载同一 Skill 返回已激活状态，不重复写入上下文或事件。

当前只有一个 Skill 时，行为保持简单：目录中只有一项，模型可按需加载。为避免功能回退，商品任务的首轮 Prompt 明确要求在执行 Skill 流程前加载匹配的 Skill。

### 3.3 Tool 暴露与安全边界

首轮不做 Skill 级动态 Tool 裁剪。商品工具数量较少，Run 仍按 AgentVersion 固定全部允许的 ToolVersion；Skill 只提供任务方法，不是授权机制。

即使模型没有加载 `price_update` Skill 而直接调用 `update_price`，PreToolUse、RBAC、业务 Policy、资源范围和写前确认仍必须完整执行。Skill 内容不得承担硬安全边界。

### 3.4 组合根归属

新增 `composition/product_operations.py` 作为商品运营 Agent 的唯一组合根，负责组装 Runtime、Agent、Skill、Tool、Policy、Backend 和 Commerce Adapter。

- `runtime/`：领域无关的执行机制；
- `agents/product_operations/`：商品 Agent 定义、Skill、Prompt、Policy 和业务 Tool；
- `adapters/commerce/`：电商平台端口实现与协议适配；
- `composition/`：把上述模块装配成可运行产品；
- `apps/worker/assembly.py`：只做兼容导出或直接从 `composition` 导入；
- `eval/`：允许依赖 `composition`，不得依赖 `apps`。

迁移只改变模块归属，不改变现有执行语义和持久化结构。

## 4. 执行流程

```text
用户选择 Workspace 并提交任务
        ↓
API 解析 Workspace 唯一 AgentVersion，创建 Session
        ↓
Runtime 创建 Run，固定 Agent / Skill / Tool / Source 版本
        ↓
Context 注入 Agent Prompt + Skill 名称和描述
        ↓
模型调用 load_skill(skill_key)
        ↓
Runtime 校验并激活固定版本的 Skill
        ↓
下一轮 Context 注入完整 Skill 指令
        ↓
模型调用商品 Tool
        ↓
PreToolUse / Tool 执行 / PostToolUse / AgentEvent
```

Session 创建后不允许切换 Agent。Workspace 后续更新 Agent 绑定，只影响新 Session。

## 5. 数据与接口调整

- Session 创建请求移除客户端必填的 `agent_id`。
- 应用服务新增“解析 Workspace 唯一有效 AgentVersion”的单一入口，供 Workspace Overview 和 Session 创建复用。
- 工作台 `TaskComposer` 不再接收或提交 Agent ID，只根据 Workspace 是否已正确配置决定能否提交任务。
- 删除不再使用的全局 `/api/agents` 产品路由；若底层管理场景未来需要 Agent 目录，应另建受 Workspace 和管理权限约束的配置接口。
- `AgentDefinition` 的 Skill 集合表示该 Agent 可见且在 Run 创建时固定版本的 Skill，不增加自动路由配置。
- 新增持久化的 Skill 激活记录或等价的 Run 事件，保证恢复后能够重建已加载 Skill，不能只保存在 Worker 内存。

## 6. 错误处理

- Workspace 未绑定 Agent：Session 创建失败，返回可诊断的配置错误。
- Workspace 存在多个有效绑定：返回 409，不选择第一项。
- `load_skill` 缺少或使用未知 `skill_key`：返回结构化 Tool 错误，不激活任何 Skill。
- 请求加载不属于当前 Run 快照的 Skill：拒绝并记录审计事件。
- Skill 正文缺失或版本无法解析：Run 失败为配置错误，不让模型在缺少流程说明时继续写操作。
- 恢复执行时，Runtime 从持久化记录恢复已激活 Skill，再继续原 Run。

## 7. 测试策略

### 确定性单元测试

- Context 首轮只包含 Skill 名称和描述，不包含完整正文；
- 加载后下一轮包含对应正文，其他 Skill 正文仍不出现；
- 未知、越界和重复 `load_skill` 行为正确；
- Skill 加载状态可持久化并在恢复后重建；
- 未加载 Skill 直接调用写 Tool 时，安全管线仍然生效。

### API 与工作台测试

- Session 创建不再要求 `agent_id`；
- 服务端只使用当前 Workspace 的唯一有效 AgentVersion；
- 零绑定和多绑定分别返回明确错误；
- 工作台不请求全局 Agent 目录，也不提交 `agent_id`；
- Workspace 未配置 Agent 时仍禁用任务输入并提示配置问题。

### 架构测试

- `runtime/` 继续禁止导入 `agents/`、`adapters/`、`apps/`、`eval/` 和 `composition/`；
- `adapters/` 不再包含产品级 Runtime 组合根；
- `composition/` 可以依赖 Runtime、Agent 和 Adapter，但不能被 Runtime 反向依赖。

## 8. 不做事项

- 不增加客服、售后或库存等第二个业务 Agent；
- 不实现 Agent 自动路由；
- 不实现独立 Skill Router、Embedding 检索或二次模型分类；
- 不按 Skill 动态裁剪 Tool；
- 不把 Skill 选择当作权限或业务安全边界；
- 不改变现有商品写操作、确认、幂等和恢复规则。

## 9. 验收标准

1. 用户只选择 Workspace，即可创建固定商品运营 AgentVersion 的 Session；
2. 前端和公开产品 API 不再暴露无实际用途的 Agent 选择；
3. 模型首轮只看到 Skill 目录，加载后才看到对应完整指令；
4. Skill 激活可审计、可恢复、版本固定；
5. 安全管线不依赖 Skill 是否加载；
6. 商品组合根不再位于 `adapters`；
7. Runtime 依赖边界、API 测试、工作台测试、Eval 与静态检查全部通过。
