# 商品运营参考 Agent 设计

> 版本：v0.1
> 日期：2026-10-02
> 状态：已确认

## 1. 目标与边界

商品运营 Agent 是 Runtime 的首个参考业务实现，用于证明同一套 Runtime 可以承载具有多店铺作用域、写操作风险、人机交互和故障恢复的业务 Agent。它不是 Runtime 内置的商品模块，也不尝试复制完整电商 ERP。

首版闭环只承诺：

- 查询 Workspace 内授权店铺的商品与渠道售卖状态；
- 对比同一商品在淘宝和抖音店铺的价格；
- 根据用户目标和当前状态生成维护建议；
- 在完成授权、业务规则和写前确认后修改单个店铺的商品价格；
- 对写入进行结果回查，并在不确定时进入 Reconciliation。

首版不实现库存调整、上下架、商品资料编辑、活动创建、订单处理和客服售后。

## 2. Runtime 与商品域的分界

| 责任 | Runtime | 商品运营 Agent / Adapter |
|---|---|---|
| ToolCall 状态、持久化与恢复 | 是 | 否 |
| JSON Schema 校验 | 是 | 提供 Schema |
| 别名、SKU、店铺归一化 | 调用扩展 | 是 |
| 资源范围与动作权限 | 是 | 通过 AuthorizationResourceResolver 将 `store` 映射为通用资源引用 |
| 成本底价、锁价和价格幅度规则 | 调用扩展 | 是 |
| Input/Confirmation Request 状态 | 是 | 提供问题、Schema 和候选项 |
| `operation_id`、幂等与回查调度 | 是 | 平台 Adapter 实现执行与回查 |
| 淘宝、抖音或 Mock 协议 | 否 | 是 |

Runtime 核心不得导入本 Agent，不出现 `SKU`、`store_id`、`price` 或平台枚举分支。

## 3. 业务实体与 Mock 权威状态

参考 Agent 在独立 `product_operations` Schema 中保存配置和 Mock 平台权威状态：

```text
stores
  id, workspace_id, platform, external_store_ref,
  name, aliases, status, version

products
  id, workspace_id, sku, title, status, version

listings
  id, workspace_id, store_id, product_id,
  external_product_ref, currency, current_price,
  cost_price, status, version, updated_at

price_locks
  id, workspace_id, listing_id, reason,
  starts_at, ends_at, version
```

关键约束：

- `(workspace_id, platform, external_store_ref)` 店铺唯一；
- `(workspace_id, sku)` 商品唯一；
- `(workspace_id, store_id, product_id)` Listing 唯一；
- 价格使用 Decimal 和明确 currency，不使用 float；
- Listing 写入使用 `version` 做乐观并发校验；
- Runtime 的 `membership_resource_scopes` 只保存 `resource_type = store` 与 Store ID，不对上述表建立商品专用逻辑。

Mock Adapter 将这些表视为外部平台的权威状态，并支持注入超时、响应丢失、暂时失败和回查延迟。未来接入真实平台时替换 Adapter，Tool Schema、Runtime 和 Eval 协议不变。

## 4. Tool 集合

### 4.1 `search_products`

- 副作用：`READ`；
- 输入：关键词、可选店铺范围；
- 输出：SKU、商品名、上架店铺和稳定资源 ID；
- 权限：`product.read`。

### 4.2 `get_product_snapshot`

- 副作用：`READ`；
- 输入：`store_id` 与 `sku`；
- 输出：当前售价、币种、状态、Listing Version 和脱敏规则摘要；
- 权限：`product.read`。

### 4.3 `compare_listing_prices`

- 副作用：`READ`；
- 输入：`sku` 和可选授权店铺集；
- 输出：各授权店铺的当前价格与状态；
- 权限：`product.read`。

### 4.4 `update_price`

- 副作用：`WRITE`；
- 模型可见输入：`store_id`、`sku`、`target_price`；
- 服务端补全：Listing ID、currency、当前 Listing Version、Policy Version、`operation_id` 和 idempotency key；
- 输出：写入前后价格、最终 Listing Version、外部引用和回查状态；
- 权限：`product.price.write`。

Listing Version、Policy Version、Actor 和幂等键不交给模型自由生成。`update_price` 的确认指纹绑定这些服务端事实。

WeKnora 检索作为独立 MCP Tool 装载，不伪装成商品 Tool。知识结果可用于解释品牌规范或生成建议，不能替代价格 Policy、权限或平台最终状态。

## 5. 参数来源与信任规则

JSON Schema 只能证明“值的结构合法”，不能证明“值是用户说的”。因此写 Tool 的关键参数还必须经过确定性 Provenance 校验。

`ArgumentProvenance` 只允许：

```text
USER_EXPLICIT
USER_CONFIRMED
SYSTEM_RESOLVED
DETERMINISTIC_DERIVED
UNTRUSTED
```

- `USER_EXPLICIT`：业务解析器能在当前用户消息中精确找到证据，并通过 Workspace 别名表确定性归一化；
- `USER_CONFIRMED`：来自已处理 Input Request 的结构化回答；
- `SYSTEM_RESOLVED`：来自当前 Workspace 的权威配置或实时读取，例如 Listing Version；
- `DETERMINISTIC_DERIVED`：可由用户明确规则和权威数值确定性计算，例如“当前价格下调 10%”；
- `UNTRUSTED`：仅来自模型 ToolCall、历史猜测或无法定位证据。

Provenance 由业务解析器和 Runtime 根据消息、Request 和权威读取生成，不接受模型自报的 `source` 或 `confidence`。

对 `update_price`：

- `store_id` 必须是 `USER_EXPLICIT` 或 `USER_CONFIRMED`，不从旧对话、单一可用店铺或模型先验中猜测；
- `sku` 必须是 `USER_EXPLICIT / USER_CONFIRMED / SYSTEM_RESOLVED`，系统解析时要求唯一匹配；
- `target_price` 可以是 `USER_EXPLICIT / USER_CONFIRMED / DETERMINISTIC_DERIVED`；
- 任一关键参数缺失、歧义或来源不可信时，返回 InputRequirement，不进入 Access Control 之后的写执行阶段。

候选店铺由商品 Agent 查询当前 Workspace 与 Membership 的可访问范围后生成，前端不接受模型自由生成的候选列表。

## 6. 价格 Policy

`update_price` 在写前至少校验：

1. Store、Product 和 Listing 属于当前 Workspace 且状态可写；
2. Actor 具有 `product.price.write` 且 Store 在当前资源范围内；
3. `target_price` 大于 0、币种一致，小数位符合渠道规则；
4. 目标价不低于当前生效的最低允许价；
5. 当前无禁止改价的活动锁；
6. 价格变动超过 Workspace 的单次硬阈值时直接拒绝；首版不引入“更强确认”或他人审批这类未定义流程；
7. 确认后 Listing Version、Policy Version、参数指纹和当前权限均未失效。

最低允许价、锁价与变动阈值是结构化业务配置，不从 Prompt、Memory 或 WeKnora 文档中动态解析为授权事实。

## 7. 写执行与回查

`update_price` Adapter 实现：

```text
execute(operation_id, store_id, sku, target_price, expected_version)
reconcile(operation_id, expected_state)
```

执行时：

1. 使用 Runtime 分配的稳定 `operation_id` 与 idempotency key；
2. 以 `expected_version` 执行乐观并发写入，版本冲突返回确定失败，不覆盖新状态；
3. 写入后重新读取 Listing，返回观测到的价格和版本；
4. 超时或响应丢失时返回 `UNKNOWN`，Runtime 调度 `reconcile`；
5. 回查只能返回 `APPLIED / NOT_APPLIED / UNKNOWN`，不以模型文本判定。

Mock Adapter 必须支持可重现的故障注入，用于验证“写入已生效但响应丢失”等恢复场景。

## 8. Context、Skill 与知识

首版只需一个发布的 `product_maintenance` Skill，说明任务方法、可用 Tool 和风险边界。它不重复存储商品事实、价格规则或 SOP 正文。

- 当前商品与价格通过 Tool 读取；
- 硬性业务规则通过 Policy 读取；
- 品牌规范和 SOP 通过 WeKnora 检索；
- 用户稳定偏好可通过 Mem0 检索，但不能恢复店铺作用域、价格和权限。

## 9. Eval 与验收

参考 Agent Eval 至少覆盖：

1. 商品查询、多店价格对比和维护建议；
2. 信息完整的合法改价；
3. 缺店铺、SKU 或目标价时的 Input Request；
4. 模型猜测合法 `store_id` 但无可信证据时的拦截；
5. 越出资源范围、缺少写权限和 `READ_ONLY` 模式；
6. 低于最低允许价、活动锁价、币种错误和版本冲突；
7. 确认前后参数、权限、Policy 或 Listing Version 变化；
8. 写入成功但响应丢失、回查暂时不一致与重复提交；
9. WeKnora、Mem0 或 Langfuse 不可用时不改变授权与写安全结果。

任务完成以 Mock 平台最终状态为准，不以 Assistant 声称“已完成”为准。危险副作用、重复逻辑写入和合法任务误拒必须分开报告。
