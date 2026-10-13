# 商品运营模块化 P1 设计

- **状态：** 已确认，待实施
- **日期：** 2026-10-13
- **依据：** `docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md`
- **前置条件：** ADR-002 P0 已实施并通过全量测试

## 1. 目标

P1 将当前以改价为中心的商品 Agent 重组为可扩展的商品运营模块化单体，增加库存调整和刊登上下架能力，同时保持 Runtime 与商品业务解耦。

完成后应满足：

1. 新增商品业务能力不修改 Runtime 主循环；
2. 价格、库存、刊登模块各自拥有明确的 Tool、Service、Policy、Gateway 和 Reconciler；
3. 外部平台差异只存在于 Adapter；
4. 所有写操作复用 Runtime 的 Operation、确认、幂等和 Reconciliation 状态机；
5. 组合根只装配对象，不包含商品规则、命令转换和按 Tool 分支；
6. Eval 使用本次运行冻结的能力版本快照，不再复制版本常量。

## 2. 范围

### 2.1 P1 包含

- 发布新的 `product-operations` Agent stable key；
- 保留旧 `product-maintenance` AgentVersion 和旧 ToolVersion，供历史 Run 恢复；
- 建立 Catalog、Pricing、Inventory、Listing 四个业务模块；
- 引入 `PricingGateway`、`InventoryGateway`、`ListingGateway`；
- 增加库存位置与库存水平数据模型；
- 增加价格设置、库存增量调整、刊登发布与取消发布；
- 增加商品应用层 `OperationReconcilerRegistry`；
- 使用已有 `BusinessPolicyVersion` 保存 Workspace 价格规则；
- 生成不可变 `EvalCapabilityManifest`；
- 为 Tool、Policy、Service、Adapter、Recovery 建立分层测试。

### 2.2 P1 不包含

- 不实现第二个真实电商平台；
- 不实现 `StorePlatformBinding` 或动态 `CapabilityRouter`；
- 不引入 Workflow Engine、LangGraph 或 Multi-Agent；
- 不实施 ADR-001 的 Skill 能力模型重构；
- 不扩展到订单、退款、客服、售后、广告和促销；
- 不拆微服务；
- 不修改简历正文或继承旧评测数字。

### 2.3 Skill 兼容例外

当前 Skill 指令硬编码了 `get_product_snapshot` 和 `update_price`。新 Agent 若只发布新 Tool，将无法执行旧 Skill。因此 P1 只发布一个兼容性 SkillVersion，将指令中的 Tool 名称更新为新 stable key；不修改 `required_skill_keys`、`required_tool_keys`、强制 `load_skill` 等第一版 Skill 语义。ADR-001 的结构性调整继续单独实施。

## 3. 模块结构

```text
agents/product_operations/
  catalog/
    models.py
    service.py
    tools.py
  pricing/
    models.py
    gateway.py
    policy.py
    service.py
    tools.py
    reconciliation.py
  inventory/
    models.py
    gateway.py
    policy.py
    service.py
    tools.py
    reconciliation.py
  listing/
    models.py
    gateway.py
    policy.py
    service.py
    tools.py
    reconciliation.py
  shared/
    errors.py
    write_contracts.py
    reconciliation.py
  persistence/
    models.py
    repository.py
  agent.py
  prompt.py
  skill.py
```

按业务能力组织变化频率相近的文件；`shared` 只放真正跨能力复用的有限契约，不演化为万能工具箱。

## 4. Tool 契约

新 `ProductOperationsAgent` 只暴露以下八个业务 Tool：

```text
search_products
get_listing
compare_listing_prices
set_listing_price
get_inventory_level
adjust_inventory_quantity
publish_listing
unpublish_listing
```

### 4.1 模型可见引用

模型可见参数使用 `store_ref` 和 `location_ref`，接受用户明确说出的名称或别名。`store_id`、`listing_id`、`location_id`、平台连接、凭证和 Adapter key 由服务端解析，不能由模型填写。

旧 ToolVersion 的 `store_id` 字段继续仅供历史 Run 恢复；新 ToolVersion 不复用该误导性字段名。

### 4.2 Catalog

`search_products(query?, store_refs?)` 搜索内部商品/SKU，返回匹配商品及其授权店铺，不执行写操作。

### 4.3 Pricing

- `compare_listing_prices(sku, store_refs?)` 由确定性代码返回价格行、最低价、最高价和价差；不同币种不得直接比较；
- `set_listing_price(store_ref, sku, target_price)` 将指定 Listing 设置为绝对目标价。

### 4.4 Inventory

- `get_inventory_level(store_ref, sku, location_ref?)` 返回一个确定的库存位置；
- `adjust_inventory_quantity(store_ref, sku, location_ref?, delta, reason)` 执行增量调整。

若店铺只有一个有效履约位置，系统可以确定性解析；存在多个位置且用户未指定时进入 `WAITING_INPUT`。P1 不向模型开放绝对覆盖式 `set_inventory_quantity`。

### 4.5 Listing

- `get_listing(store_ref, sku)` 返回 `ListingSnapshot`；
- `publish_listing(store_ref, sku)` 发布刊登；
- `unpublish_listing(store_ref, sku, reason?)` 停止刊登但保留内部对象和历史记录。

规范发布状态只有 `PUBLISHED`、`UNPUBLISHED`、`UNKNOWN`。平台审核、违规或资料缺失作为 issues 或规范错误返回。库存为零不等价于取消发布。

## 5. 执行职责

### 5.1 PreToolUse

负责：

- Schema 校验和参数规范化；
- 参数来源；
- 缺失输入识别；
- Workspace、店铺和履约位置范围；
- RBAC；
- 业务规则预检；
- ExecutionMode 与人工确认。

### 5.2 Application Service

负责：

- 重新读取最新业务状态；
- 再次执行领域 Policy；
- 校验业务不变量和 `expected_version`；
- 构造服务端可信 Command；
- 调用 Gateway；
- 将平台回执映射为模型可见结果。

PreToolUse 的预检不能代替 Service 的最终校验。Policy 组件提供预检和执行校验两个入口，共享同一版本化配置，避免复制规则。

### 5.3 Gateway 和 Adapter

业务模块拥有 Gateway Port。一个平台 Adapter 可以实现多个 Gateway，但业务模块不依赖 `adapters` 包。

```python
class PricingGateway(Protocol):
    async def get_price(self, query: PriceQuery) -> PriceSnapshot: ...
    async def set_price(self, command: SetListingPriceCommand) -> WriteReceipt: ...
    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult: ...

class InventoryGateway(Protocol):
    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot: ...
    async def adjust_inventory(self, command: AdjustInventoryCommand) -> WriteReceipt: ...
    async def reconcile_inventory(self, command: ReconcileInventoryCommand) -> ReconcileResult: ...

class ListingGateway(Protocol):
    async def get_listing(self, query: ListingQuery) -> ListingSnapshot: ...
    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt: ...
    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt: ...
    async def reconcile_listing(self, command: ReconcileListingCommand) -> ReconcileResult: ...
```

## 6. 写操作与恢复

价格、库存和刊登写入共用：

```text
WriteReceipt.status = APPLIED | NOT_APPLIED | UNKNOWN | FAILED
```

Runtime 在外部调用前持久化 Operation。`Operation.expected_state` 保存规范化、可回放的业务预期：

- 价格：目标价格、币种、资源版本、Policy 版本；
- 库存：调整量、调整前数量、预期调整后数量、位置、资源版本和原因；
- 刊登：预期发布状态、资源版本和原因。

模型不能提供 `expected_state`。商品应用层在 prepare 阶段根据已固定的 ToolVersion 写入受控 `operation_type`，恢复时再按该值将 Operation 分发给对应 Reconciler；不能根据模型参数选择 Reconciler。Runtime 只处理 `APPLIED`、`NOT_APPLIED`、`UNKNOWN` 三种回查结论。

为保证 `expected_state` 确实早于外部副作用落库，写 Handler 使用通用的 prepare/execute 两阶段契约：

```text
handler.prepare(normalized_arguments)
  → 解析可信内部资源、读取版本、执行最终业务校验
  → 返回 PreparedToolWrite(command, expected_state)

Runtime OperationService.begin(expected_state)
  → 提交 Operation 和 EXECUTE Attempt

handler.execute_prepared(operation_id, idempotency_key, command)
  → 执行外部写入
```

`prepare` 不得产生外部副作用；`execute_prepared` 不得重新采用模型参数构造目标。进程在 prepare 后、Operation 提交前崩溃时没有外部写入；Operation 提交后崩溃则进入既有 UNKNOWN/Reconciliation 语义。

响应丢失后不得直接重复价格、库存或刊登写入。库存增量尤其不能盲目重复执行。

## 7. 数据模型

新增：

```text
FulfillmentLocation
- id
- workspace_id
- store_id
- external_location_ref
- name
- aliases
- status
- version

InventoryLevel
- id
- workspace_id
- location_id
- product_id
- available_quantity
- version
- updated_at
```

约束：

- `(workspace_id, store_id, external_location_ref)` 唯一；
- `(workspace_id, location_id, product_id)` 唯一；
- `available_quantity >= 0`；
- 所有更新使用 CAS 递增 `version`。

Listing 状态从第一版 `ACTIVE/INACTIVE` 迁移为 `PUBLISHED/UNPUBLISHED`，历史数据执行显式映射，不静默丢弃。

## 8. 配置与版本

### 8.0 Registry 多版本前置修正

当前 Run 已固定精确 AgentVersion、SkillVersion 和 ToolVersion，但进程内 Registry 仍拒绝同一 stable key 的多个版本并存。P1 发布新版本后，若不先修正该问题，Worker 无法同时服务新 Run 和恢复旧 Run。

P1 先完成一个通用 Runtime 修正：

- Registry 按 `(stable_key, version)` 与 `version_id` 保存全部已装载版本；
- `find_by_key` 只返回显式选定的当前版本；
- `find_by_version_id` 可以返回兼容版本；
- ToolBinding 按 `tool_version_id` 绑定 Handler、PreToolProfile 和执行 Profile；
- Run 创建只解析当前版本，Run 恢复严格使用已固定 Version ID；
- 相同 `(stable_key, version)` 指向不同 Version ID 时启动失败。

该修正只处理通用版本选择和恢复，不包含任何商品字段、商品策略或平台路由。

### 8.1 Agent 和 Tool

- 新 Agent stable key：`product-operations`；
- 新 Agent 名称：`ProductOperationsAgent`；
- 旧 `product-maintenance` 保留为兼容版本；
- 新 Tool stable key 发布新 ToolVersion；
- 新 Agent 只固定新 ToolVersion；
- Worker 为历史 Run 注册旧 Handler/ToolVersion，但不向新 Run 暴露。

### 8.2 PricePolicyConfig

使用已有 `business_policies` 和 `business_policy_versions`：

- stable key：`product-pricing`；
- 配置 JSON 由 `PricePolicyConfig` 严格解析；
- 只装配 `PUBLISHED` 版本；
- Run 记录 `RunPolicySnapshot`；
- 确认请求和 Operation 记录 Policy 版本或哈希。

### 8.3 EvalCapabilityManifest

评测开始时从实际发布并绑定的能力生成不可变 Manifest，至少记录：

```text
agent_version_id
prompt_version
skill_version_ids
tool_version_ids
model_config_version_id
model_snapshot
policy_version_ids
dataset_hash
scorer_version
```

整个 Eval Run 复用同一个 Manifest。不得在用例之间读取“最新发布版本”。

## 9. 测试策略

每个模块覆盖：

1. Tool Contract：Schema、模型可见字段和错误结构；
2. Policy Contract：权限、范围、确认、业务规则和版本冲突；
3. Service Test：完整业务用例；
4. Adapter Contract：支持该能力的 Adapter 运行同一套一致性测试；
5. Recovery Test：成功、确定失败、写后丢响应、写前超时、持续 UNKNOWN、重复执行和版本变化。

架构测试继续保证 Runtime 不导入商品模块，组合根不包含业务分发和领域规则。

## 10. 验收标准

1. 新 Agent 只暴露八个冻结 Tool stable key；
2. 历史 Run 仍能按旧 AgentVersion/ToolVersion 恢复；
3. `composition/product_operations.py` 不再包含领域策略、平台命令转换或按 Tool 分支；
4. 四个模块可以独立测试；
5. 库存和刊登写入具备与改价相同的确认、幂等和恢复语义；
6. 新写操作在响应丢失后不会产生重复副作用；
7. Eval 报告记录冻结的真实能力版本；
8. 全量 pytest、ruff、mypy 通过；
9. ADR-002 更新为 P1 已实施前，以上验收必须全部满足；
10. 旧真实模型评测数字不自动沿用，P1 完成后重新评测。
