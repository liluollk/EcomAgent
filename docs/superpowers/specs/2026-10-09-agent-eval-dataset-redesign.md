# Agent 评测集重构设计

## 1. 目标与定位

本次重构把商品运营 Agent 的评测定位为：**在可控商品业务环境中，对真实模型进行小规模、可复现、失败驱动的行为评测**。

评测用于比较同一项目不同版本、发现失败并验证修复，不把 Mock 业务环境结果描述为真实线上收益，也不宣称小样本能够代表完整用户分布。

本次只重构评测数据的用途、执行口径和报告主指标，不重写 Runtime、Agent Runner、业务 Tool 或确定性恢复套件。

## 2. 与企业级项目的差异

企业级 Agent Runtime 同时承载商品、库存、活动、订单、客服和售后等业务，评测重点通常是统一执行链路、权限审批、事件、恢复和跨业务治理。澄清是业务 Agent 的常规行为，不一定成为平台叙事的重点。

本项目是个人项目，业务范围收敛在商品查询、价格对比和单商品改价。评测不复制企业级指标矩阵，而是用一个完整参考 Agent 证明以下能力：

- Agent 能理解正常商品任务并选择合适的 Tool；
- 信息不足时不会猜测关键业务事实；
- Tool 写入经过确定性的权限、Policy 和 Runtime 执行边界；
- Runtime 能处理确认、幂等、未知写入和恢复。

## 3. Skill、澄清与确认的边界

改价继续作为 `product_maintenance` Skill，而不是固定业务 Workflow。

- Skill 描述查询、比较、澄清、计划和调用 Tool 的任务方法；
- Agent 根据用户请求决定实际步骤，不要求所有任务经过同一固定工具序列；
- `update_price` 是原子写 Tool；
- Business Policy 强制执行授权范围和价格规则；
- Runtime 根据 `ExecutionMode` 处理确认、幂等、执行和 reconciliation。

澄清与确认必须区分：

- **澄清**用于补齐执行任务所必需的业务事实，例如 `store_id`、SKU 或目标价。业务侧负责识别缺失或歧义，Runtime 只持久化 Input Request 并恢复原 Run。即使使用 `AUTO_EXECUTE`，必要澄清仍然存在。
- **确认**用于授权已经明确的写操作，由 Runtime 根据当前 `ExecutionMode` 决定是否创建 Confirmation Request。`AUTO_EXECUTE` 能力评测不插入确认交互，但仍执行权限和 Business Policy 校验。

澄清只作为正常任务的一种形态和成功条件，不单独生成“澄清准确率”。

## 4. 数据集结构

商品评测数据调整为四类：

```text
eval/datasets/product_operations/
├── development.yaml
├── regression.yaml
├── held_out.yaml
├── safety.yaml
└── manifest.json
```

### 4.1 `development.yaml`

用于日常调试和失败定位，只包含合法、无故障注入的业务请求：

- 商品查询；
- 单商品改价；
- 跨店价格对比；
- 查询后改价的复合任务；
- 缺少必要信息时的澄清与恢复；
- 多轮补充目标和约束。

开发样例可以被查看和修改，不产生对外使用的最终成绩。

### 4.2 `regression.yaml`

只收录真实运行中出现、已经完成修复的 Bad Case。每条用例必须在评测文档中记录：

- 原始失败现象；
- 对应 Trace 或报告；
- 失败归因；
- 修复位置；
- 修复后的期望结果。

没有真实失败证据时不为凑数量制造回归用例。回归集用于防止旧问题复发，不用于宣称泛化能力。

### 4.3 `held_out.yaml`

由人工检查过的 Gold Case 构成，只包含合法、无故障注入的正常业务任务。它不参与开发阶段调试，用于生成最终任务完成率。

保留任务必须同时隔离：

- 用户表达模板；
- 店铺、SKU 和价格实体；
- 任务步骤组合；
- 单轮与多轮表达方式。

只替换店铺、SKU 或数字不构成新的保留任务。根据保留集失败继续修改实现后，当前保留集即视为已污染；下一次正式比较必须建立新版本。

### 4.4 `safety.yaml`

单独保存：

- 无写权限时要求改价；
- 访问无权店铺；
- 尝试绕过价格 Policy；
- Prompt Injection 要求忽略权限或规则；
- 请求泄露没有 Tool 证据或无权访问的信息。

安全用例不进入正常任务完成率分母。

## 5. Gold Case、Baseline 与 Bad Case

Gold Case 是用例质量标准，不是额外数据分区。一条可执行 Gold Case 至少包含：

- 用户请求；
- 初始业务状态；
- Actor 权限和店铺范围；
- 预期最终状态；
- 必要的回答证据；
- 允许和禁止的副作用。

Baseline 是固定模型、Prompt、Skill、Tool Schema、Scorer、初始状态、温度和运行次数后得到的首个可用版本结果。后续版本只在协议一致时比较。

Bad Case 是真实执行失败。失败完成归因和修复后，转入 `regression.yaml`，形成：

```text
运行 → 发现 Bad Case → 查看 Trace → 归因 → 修复 → 回归锁定
```

## 6. 执行与判分

商品能力评测固定使用 `AUTO_EXECUTE`，避免人工确认模式干扰 Agent 能力结果。`AUTO_EXECUTE` 只跳过人工确认，不跳过权限、参数、Business Policy 和资源版本校验。

任务完成以权威业务状态为主：

- 目标 Listing 达到期望状态；
- 不相关 Listing 保持不变；
- 逻辑写入次数不超过期望；
- Run 正常完成；
- 查询类回答中的关键事实能在 ToolResult 或最终状态中找到证据。

Tool 轨迹用于两类目的：

1. 失败归因；
2. 验证必须发生或绝对禁止的过程不变量。

评测不要求所有成功任务走完全相同的工具顺序。只有安全和恢复边界要求固定先后关系，例如未知写入必须先 reconciliation，不能直接重试。

## 7. 核心结果与独立门槛

### 7.1 商品能力

简历和面试只使用一个主指标：

```text
任务完成率 = 成功执行次数 / 纳入能力评测的总执行次数
```

结果必须同时报告任务数、每任务重复次数、成功次数和总执行次数，不能只写百分比。Infrastructure Error 单独报告，不计为模型质量结果，也不能被当作成功。

### 7.2 安全

安全报告至少区分：

- 模型提出危险调用；
- Runtime 或 Policy 成功阻止；
- 实际发生危险副作用。

实际未授权写入必须为 0。安全结果作为独立门槛，不与任务完成率合并成综合分数。

### 7.3 Runtime 恢复

现有确定性 Runtime 与 Recovery 套件继续验证：

- Input Request 和 Confirmation Request 的等待与恢复；
- UNKNOWN 后先回查；
- 响应丢失时不盲目重写；
- 幂等重放不产生第二次逻辑写；
- 最终状态和事件记录一致。

这些结果是系统不变式验证，不包装成真实用户任务完成率。

## 8. 数据规模

首版目标规模保持克制：

- Development：12 至 15 条合法业务任务；
- Regression：只保留有真实失败证据的用例，不设最低数量；
- Held-out：12 至 15 条模板和实体共同隔离的合法业务任务；
- Safety：6 至 8 条越权、规则绕过和敏感信息任务；
- Runtime Recovery：沿用已有确定性故障用例。

同一 Held-out Case 使用固定配置重复运行 3 次。重复运行用于观察稳定性，不把多次执行包装成更多独立业务场景。

## 9. 实现约束

- Development 与 Held-out 只包含合法、无故障注入的能力任务；
- Safety 只包含权限、业务规则、Prompt Injection 和敏感信息场景；
- 正常查询任务通过最终状态断言要求零写，不进入安全分母；
- 能力评测不声明 confirmation interaction，必要业务输入使用 input interaction；
- `product_maintenance` Skill 和 Agent Prompt 只描述业务方法，是否确认由 Runtime 的 `ExecutionMode` 决定；
- Regression 中的每条案例必须关联真实失败证据；
- Baseline 只来自当前固定协议下的有效 Held-out 报告。

## 10. 非目标

本次不做以下事项：

- 不增加 Token、延迟、成本或综合总分；
- 不引入 LLM-as-Judge；
- 不建设通用 Workflow 引擎；
- 不把 Mock 环境结果描述为生产指标；
- 不为了数据量生成没有真实来源的回归案例；
- 不在本次重构中修改 Runtime 状态机或增加新的商品业务能力。

## 11. 验收标准

1. Product Eval 支持 `development`、`regression`、`held_out` 和 `safety` 四种用途；
2. Development 与 Held-out 不包含故障注入、安全对抗或 confirmation interaction；
3. 缺参任务在 `AUTO_EXECUTE` 下仍能通过 Input Request 补齐信息并恢复原 Run；
4. Held-out 与 Development 的表达模板、实体和任务组合存在可审查的隔离；
5. Safety 不进入正常任务完成率分母，实际危险副作用保持硬门槛；
6. Regression 中每条用例都能关联真实失败记录，不设置数量下限；
7. Runtime 和 Recovery 套件继续独立运行，商品真实模型报告不重复计算其故障恢复指标；
8. manifest、哈希、协议指纹、CLI、测试和中文评测文档使用同一套命名与口径；
9. Baseline 只从当前固定协议下有效的 Held-out 报告派生；
10. 所有修改不改变 Runtime 与商品业务的依赖边界。
