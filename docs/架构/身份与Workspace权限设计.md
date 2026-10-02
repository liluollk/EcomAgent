# 身份与 Workspace 权限设计

> 版本：v0.1
> 日期：2026-10-02
> 状态：待评审

## 1. 目标与边界

本文定义首版的身份来源、Workspace 成员角色、店铺数据范围、动作权限、Actor 在 API 与 Worker 之间的传递、凭证管理和模型端点安全。

首版不接入 Keycloak、企业 SSO、组织目录和跨企业多租户计费。未接入企业身份系统不代表省略身份边界；所有状态变化和写 Tool 仍必须关联可审计的 Actor。

## 2. 身份来源

首版使用本地开发账号和服务端签发的短期 JWT：

- 用户使用本地账号登录，密码仅保存强哈希；
- API 校验 JWT 后构造不可由前端覆盖的 `ActorContext`；
- JWT 至少包含 `actor_id`、签发时间、过期时间和 token ID，不直接携带可长期信任的角色与资源范围；
- Workspace 成员关系、角色和范围以 PostgreSQL 当前数据为准；
- 本地开发可以提供预置账号，但不能使用任意受信任 Header 冒充用户；
- 未来接入 SSO 时只替换登录和身份映射，Runtime 继续消费相同 ActorContext。

`ActorContext` 至少包含：

```text
actor_id
workspace_id
membership_id
role
resource_scopes
request_id
```

## 3. 最小角色

首版只定义两个角色：

### 3.1 WorkspaceOwner

WorkspaceOwner 可以：

- 查看和修改 Workspace 配置；
- 邀请、移除成员和修改成员角色；
- 配置成员资源范围与动作权限；商品运营 Agent 中资源为店铺；
- 配置 Agent、模型连接、Skill、Source、MCP、知识库和业务规则；
- 修改 Execution Mode；
- 创建和使用 Session；
- 回答本人有权访问的 Input Request；
- 确认本人发起且本人仍有执行权限的写操作；
- 查看 Workspace 审计记录。

### 3.2 Operator

Operator 可以：

- 查看被授权的 Workspace、店铺和业务 Agent；
- 创建和使用 Session；
- 在资源范围和动作权限内调用只读或写 Tool；
- 回答自己可访问 Session 的 Input Request；
- 在具有写动作权限时确认自己发起的写操作；
- 查看自己可访问的 Run 与执行事件。

Operator 默认不能管理成员、模型凭证、Source、Skill、业务规则或 Execution Mode。

首版不实现 SKU 级角色或资源授权，也不实现由另一名成员审批写操作。Confirmation Request 是当前操作者的写前确认。

## 4. 权限模型

授权结果由三个维度共同决定：

```text
成员角色
  ∩ 资源数据范围
  ∩ 动作权限
```

### 4.1 资源数据范围

Runtime 不硬编码“店铺”概念，只处理 `(resource_type, resource_id)`。每个 Membership 在某类资源上的范围是：

- Workspace 内该类资源全部对象；或
- 明确的资源 ID 集合。

商品运营 Agent 将 `resource_type = store` 映射到自己的店铺实体。ToolBinding 使用确定性 ResourceResolver 从规范化参数提取资源引用，Runtime 只负责根据当前 Membership 校验。候选店铺列表只能从当前 Membership 可访问的店铺中生成。用户输入了格式合法但越出范围的 `store_id` 时必须拒绝，不能转成确认请求。

### 4.2 动作权限

首版权限至少包括：

| 权限 | 用途 |
|---|---|
| `workspace.read` | 查看 Workspace 配置 |
| `workspace.manage` | 修改 Workspace 配置 |
| `membership.manage` | 管理成员与范围 |
| `agent.configure` | 配置 Agent、Skill 与 Source |
| `model.configure` | 配置模型端点与凭证 |
| `policy.configure` | 配置业务规则和 Execution Mode |
| `session.create` | 创建 Session 与 Run |
| `run.read` | 查看可访问的 Run 与事件 |
| `product.read` | 查询授权店铺的商品 |
| `product.price.write` | 修改授权店铺的商品价格 |

Execution Mode 不授予动作权限：拥有 `product.price.write` 但 Workspace 为 `READ_ONLY` 时仍不能执行；Workspace 为 `AUTO_EXECUTE` 但成员缺少写权限时也不能执行。

首版不单独定义 `write.confirm` 权限。能够确认某次写操作的前提是：当前成员就是该操作绑定的 Actor，并且仍拥有对应业务写权限。单独的确认权限只在未来引入他人审批时才有意义。

## 5. Workspace 隔离

所有 Workspace 资源表必须包含 `workspace_id`，包括 Session、Message、Run、RunAttempt、RunJob、ToolCall、Input Request、Confirmation Request、AgentEvent、Skill 配置、Source 配置、模型配置、Secret 引用和审计记录。

Repository 方法必须显式接收 `workspace_id`，查询条件同时包含 Workspace 与资源 ID。禁止先按全局资源 ID 查询后再在业务层补权限判断。

首版 Session 为创建者私有：只有创建者能读取消息、提交后续输入或处理 Request。WorkspaceOwner 可查看必要的脱敏审计事件，不因角色自动获得完整会话内容。首版不实现共享 Session。

首版使用应用层隔离和自动化测试，不要求 PostgreSQL RLS；未来若增加真正多租户部署，再评估数据库 RLS。

## 6. Actor 传递与 Worker 授权

创建 Run 时持久化：

- `created_by_actor_id`；
- 创建时的 Membership ID；
- 创建时的资源范围和动作权限摘要，用于审计；
- 当前 Execution Mode 和相关配置版本。

Worker 不能使用“系统管理员”身份绕过原操作者权限。每次写操作前，Worker 根据 `created_by_actor_id` 重新读取当前 Membership、资源范围和动作权限：

- Membership 已删除或禁用：拒绝执行；
- 目标资源已移出范围：拒绝执行；
- 写权限已撤销：拒绝执行；
- Execution Mode 或 Business Policy 变化：按当前规则重新决策；
- Actor 快照只用于审计，不能覆盖当前权限事实。

系统 Actor 仅用于租约回收、Trace 补偿、Memory 同步和 Reconciliation 调度等维护任务。系统 Actor 可以推进状态机，但不能代替用户通过写权限或写前确认。

## 7. Request 处理权限

Input Request 的回答者必须：

- 是当前 Workspace 的有效成员；
- 有权访问关联 Session；
- 对候选资源具有当前数据范围；
- Request 仍为 `PENDING` 且未过期。

Confirmation Request 只能由绑定的 `actor_id` 处理。WorkspaceOwner 不能替另一个 Operator 完成首版的自确认；如未来需要他人审批，应单独设计 Approval Request，不能复用 Confirmation Request。

处理 Request 时使用条件更新保证单次消费。重复提交返回已经保存的结果，不创建第二个 RunJob。

## 8. Secret 管理

模型 API Key、MCP 凭证和平台凭证保存在独立 Secret 表：

- 配置表只保存 `secret_id`，不保存明文；
- 使用 AES-GCM 加密，主密钥从进程环境变量 `RUNTIME_MASTER_KEY` 注入，不写入数据库或仓库；
- 加密记录保存 key version、nonce、ciphertext 和认证标签；
- API 创建或更新后不再返回明文，只显示固定掩码和更新时间；
- 只有 WorkspaceOwner 可以创建、替换或删除 Workspace Secret；
- Worker 按最小需要解密，明文只存在于调用期间；
- 日志、异常、AgentEvent、ToolResult、Langfuse Trace 和 Eval 报告统一脱敏；
- 主密钥轮换通过 key version 支持逐条重加密。

## 9. 外部端点安全

允许前端填写任意模型或 MCP URL 会造成 SSRF、API Key 外泄和内网探测。首版采用部署级端点 allowlist：

- WorkspaceOwner 只能选择或填写匹配 `MODEL_ENDPOINT_ALLOWLIST` 的 HTTPS 端点；
- MCP Source 必须匹配独立的 `MCP_ENDPOINT_ALLOWLIST`；WeKnora 通过 MCP 接入时适用同一规则；
- 远程 MCP 只允许 HTTPS 传输；本地 stdio MCP 只能选择部署者预先注册的 Server Key，前端不能提交任意可执行文件、命令行参数或环境变量；
- 本地开发如需 `http://localhost`，必须通过独立开发配置显式开启；
- 禁止 URL 中携带用户名、密码或 fragment；
- 默认禁止自动跟随跨主机重定向；
- DNS 解析后校验目标地址，拒绝云元数据地址、链路本地地址和未在 allowlist 中的私网地址；
- 每个 Secret 绑定 `workspace_id`、连接类型和规范化端点，不能被另一个端点复用；
- “测试连接”接口只返回成功、错误分类和脱敏信息，不能回显响应 Header 或凭证。

这套限制保护的是运行主机和模型凭证，不属于 AgentBackend 的模型能力抽象。

## 10. 审计

以下行为必须记录审计事件：

- 登录成功和失败；
- 成员加入、移除、角色和资源范围变化；
- 动作权限与 Execution Mode 变化；
- 模型、Source、Skill、Secret 和业务规则配置变化；
- Input Request 回答；
- Confirmation Request 接受、拒绝、过期和取消；
- 写 Tool 被允许、拒绝、执行、回查或进入 `UNKNOWN`；
- Worker 以原 Actor 身份重新校验权限的结果。

审计记录至少包含 `audit_id`、`workspace_id`、`actor_id`、动作、目标类型与 ID、结果、原因码、时间和关联的 `request_id/run_id/tool_call_id`。审计 payload 必须脱敏且只追加，不允许普通业务 API 修改。

## 11. 验收用例

以下确定性用例必须全部通过：

1. Operator 无法管理成员、模型 Secret 和 Execution Mode；
2. WorkspaceOwner 可以管理配置，但不能替其他成员完成 Confirmation；
3. Operator 只能读取和修改授权资源，商品运营 Agent 中具体表现为授权店铺；
4. 格式合法但越出店铺范围的 `store_id` 被拒绝；
5. `READ_ONLY` 下任何成员都不能执行写 Tool；
6. `AUTO_EXECUTE` 不会绕过缺失的 `product.price.write`；
7. Actor 在确认后被撤销写权限，恢复时写操作被拒绝；
8. Worker 不能使用系统 Actor 绕过原 Actor 权限；
9. 使用另一个 Workspace 的资源 ID 无法读取或修改数据；
10. API Key 创建后不回显，且不会出现在日志、AgentEvent 或 Trace；
11. 未在 allowlist 的模型或 MCP URL、云元数据地址和跨主机重定向被拒绝；
12. 所有成员、配置、确认和写操作变化都有可关联的审计记录。
13. Operator 无法读取或继续另一名成员的私有 Session，WorkspaceOwner 也只能通过审计界面查看脱敏记录。
