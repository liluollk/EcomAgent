# Workspace 工作台收口设计

## 1. 背景

项目的核心定位是电商运营 Agent，其中 Runtime 负责安全、稳定、可恢复地执行 Agent，商品运营模块负责店铺和商品业务，工作台负责向运营成员呈现真实的 Workspace 状态。

当前实现存在三类错位：

- 后端执行模式为 `READ_ONLY / CONFIRM_WRITES / AUTO_EXECUTE`，前端却定义为 `AUTO_EXECUTE / AWAIT_CONFIRMATION`；
- 后端角色为 `WORKSPACE_OWNER / OPERATOR`，前端使用 `OWNER / OPERATOR`，并在 `App.tsx` 中固定传入 `OPERATOR`；
- Workspace 页面展示店铺、Agent、Model、Skill、Source 和成员，但基础详情接口只返回 Workspace 自身字段，前端测试 fixture 掩盖了真实接口缺失。

本次修复不采用局部类型替换，而是建立正式的 Workspace 工作台读取模型，使页面只展示数据库中的真实配置，并保持 Runtime 与商品业务解耦。

## 2. 目标

1. 为 Workspace 工作台提供单一、明确、可测试的聚合读取接口。
2. 统一前后端的执行模式和成员角色语义。
3. 由认证成员身份决定界面权限，不允许前端猜测或写死角色。
4. 新建任务时只使用当前 Workspace 已启用的商品运营 Agent。
5. 用两个 Workspace 的集成测试证明成员、店铺和配置不会跨 Workspace 泄漏。

## 3. 非目标

本阶段不实现以下内容：

- 不修改 Agent Runtime 的 Run、ToolCall、PreToolUse、恢复或事件机制；
- 不把商品店铺模型移动进 `runtime/`；
- 不接通业务规则到改价执行链；
- 不实现 MCP、WeKnora 或 Mem0 的真实能力装载；
- 不重做 Settings 页面和全部配置编辑表单；
- 不增加 Multi-Agent 选择、通用工作流或新的基础设施。

上述内容按后续独立阶段处理，避免 Workspace 页面收口与执行链改造互相污染。

## 4. 分层设计

```text
React Workspace 页面
          ↓
GET /api/workspaces/{workspace_id}/overview
          ↓
WorkspaceOverviewService（apps/api 应用层）
    ├── runtime：Workspace、当前成员、权限、执行模式、配置版本
    ├── runtime：WorkspaceAgentBinding、Model、Skill、Source、BusinessPolicy
    └── agents/product_operations：当前 Workspace 的 Store
```

### 4.1 Runtime 边界

`runtime/` 继续保持业务无关，不允许导入 `agents/`、`adapters/`、`apps/` 或 `eval/`。`WorkspaceService` 只负责 Runtime 自身的 Workspace 配置读写，不负责拼装商品店铺。

### 4.2 API 应用层

在 `apps/api/services/workspace_overview.py` 新增 `WorkspaceOverviewService`。应用层可以调用 Runtime 查询并读取商品运营模块的数据，负责生成面向工作台的只读页面模型。

聚合服务必须从 `WorkspaceActorDep` 获得当前成员身份，并在所有查询中显式使用 `workspace_id`。它不能接受前端传入的角色，也不能根据是否出现配置数据推断角色。

### 4.3 前端边界

前端只消费聚合接口，不再以空数组或伪造默认值把缺失字段伪装成合法响应。响应结构不合法时进入明确的加载失败状态。

页面仍可通过已有独立写接口修改执行模式。保存成功后重新拉取 overview，保证页面展示后端权威状态。

## 5. API 设计

新增：

```http
GET /api/workspaces/{workspace_id}/overview
```

响应结构：

```json
{
  "workspace": {
    "id": "uuid",
    "name": "品牌 A",
    "status": "ACTIVE",
    "execution_mode": "CONFIRM_WRITES",
    "state_version": 3
  },
  "current_membership": {
    "id": "uuid",
    "role": "WORKSPACE_OWNER",
    "permissions": ["product.price.write"],
    "resource_scopes": {
      "STORE": {
        "mode": "ALL",
        "resource_ids": []
      }
    }
  },
  "stores": [
    {
      "id": "uuid",
      "name": "淘宝旗舰店",
      "platform": "TAOBAO",
      "status": "ACTIVE"
    }
  ],
  "agent": {
    "id": "uuid",
    "stable_key": "product-maintenance",
    "name": "商品运营 Agent",
    "version": 1,
    "status": "PUBLISHED"
  },
  "model_connections": [],
  "skills": [],
  "sources": [],
  "business_policies": []
}
```

字段语义：

- `current_membership` 必须来自当前 JWT 对应用户在该 Workspace 的有效成员记录；
- `stores` 只返回当前 Workspace 内且当前成员有权访问的店铺；
- `agent` 表示当前 Workspace 启用的商品运营 Agent 版本；没有有效绑定时返回 `null`；
- 配置列表只返回摘要和状态，不返回 Secret、Skill 正文、Source 凭据或业务规则完整内容；
- `execution_mode` 和 `role` 使用后端枚举原值，前端不得再定义不同含义的别名。

保留现有 `GET /api/workspaces/{workspace_id}` 作为基础 Workspace 资源接口，不让其承担业务聚合职责。

## 6. Agent 绑定与任务创建

当前工作台通过全局 `/api/agents` 获取全部 Agent，并默认选择第一项，这与 Workspace 配置不一致。

修复后：

1. `TaskComposer` 从 overview 的 `agent` 字段取得 Agent ID，不再请求全局 Agent 列表；
2. `agent == null` 时禁用任务提交并显示“当前 Workspace 未启用商品运营 Agent”；
3. `POST /api/sessions` 除校验 Agent 存在外，还必须校验请求的 Agent 版本已在目标 Workspace 中启用；
4. E2E seed 显式创建并启用 `WorkspaceAgentBinding`，不能依赖全局 Agent 恰好只有一条。

项目仍然只有一个商品运营 Agent。这里的绑定用于保证 Workspace 配置、版本快照和任务执行一致，不把产品定位扩展成 Multi-Agent 平台。

## 7. 前端模型

前端类型与后端保持一致：

```ts
type ExecutionMode = "READ_ONLY" | "CONFIRM_WRITES" | "AUTO_EXECUTE";
type WorkspaceRole = "WORKSPACE_OWNER" | "OPERATOR";
```

`WorkspaceContainer` 负责加载 overview 和保存执行模式；`WorkspacePage` 只负责展示。Owner 权限通过 `current_membership.role === "WORKSPACE_OWNER"` 判断。

删除以下兼容逻辑：

- `AWAIT_CONFIRMATION`；
- `OWNER`；
- `currentRole="OPERATOR"`；
- `as ExecutionMode` 强制类型转换；
- 缺少关键字段时自动填充空数组或默认执行模式。

## 8. 错误处理

- 非成员访问沿用现有 `403 PERMISSION_DENIED`，不泄漏 Workspace 是否存在；
- Workspace 不存在返回现有 `404 NOT_FOUND`；
- 没有启用 Agent 属于可展示配置状态，overview 返回 `agent: null`，不返回服务器错误；
- 多个有效商品 Agent 绑定属于配置不变量破坏，返回 `409 WORKSPACE_AGENT_CONFIGURATION_INVALID`，不能静默选择第一项；
- 单个配置列表为空是合法状态，返回空数组；数据库查询失败按统一 API 错误响应处理。

## 9. 测试设计

### 9.1 API 集成测试

- Owner 请求 overview，返回 `WORKSPACE_OWNER` 和三种执行模式中的数据库真实值；
- Operator 请求 overview，返回 `OPERATOR`；
- 两个 Workspace 各自拥有不同店铺、成员和配置时，响应只包含当前 Workspace 数据；
- `SELECTED` 店铺范围只返回授权店铺，`ALL` 返回全部有效店铺；
- 没有 Agent 绑定时返回 `agent: null`；
- 多个有效商品 Agent 绑定时返回确定性 409；
- 非成员访问返回 403。

### 9.2 Session API 测试

- 已启用 Agent 可以创建 Session；
- 未绑定或已禁用的 Agent 不能创建 Session；
- 其他 Workspace 的 Agent 绑定不能用于当前 Workspace。

### 9.3 前端测试

- 页面渲染 overview 的真实 snake_case 响应；
- 三种执行模式均可展示，Owner 可修改，Operator 不显示编辑入口；
- 没有 Agent 时禁用任务提交；
- 新建任务直接使用 overview 中的 Agent ID，不请求 `/api/agents`；
- 响应缺少关键字段时显示加载失败，而不是渲染 fixture 式空页面。

### 9.4 端到端测试

- Owner 登录后看到真实店铺与 `CONFIRM_WRITES`，可以提交任务；
- Operator 登录后能查看但不能修改执行模式；
- 在两个 Workspace 间切换时，店铺和配置内容随 Workspace 正确变化且不串数据。

## 10. 验收标准

1. 前后端代码中不再出现 `AWAIT_CONFIRMATION` 或前端角色 `OWNER`。
2. `App.tsx` 不再写死成员角色。
3. Workspace 页面展示内容全部来自 `/overview` 的数据库真实响应。
4. TaskComposer 不再调用全局 `/api/agents` 或选择数组第一项。
5. Session 创建强制校验 Workspace Agent 绑定。
6. 两个 Workspace 的隔离测试通过。
7. 后端相关 pytest、前端 Vitest、TypeScript build 和目标 Playwright E2E 全部通过。
8. `runtime/` 的依赖方向测试继续通过，且 `runtime/` 未引入商品运营模型。
