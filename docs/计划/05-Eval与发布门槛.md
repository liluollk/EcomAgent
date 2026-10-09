# Eval 与发布门槛实施方案

## 1. 目标

在受控商品业务环境中验证三件事：商品 Agent 能否完成用户任务，安全边界能否阻止违规写入，Runtime 能否在异常后正确恢复。三类结果独立报告，不合成综合分数，也不表述为生产业务指标。

## 2. 评测结构

商品 Agent 数据集分为四类：

- `development`：合法任务的日常调试集，可迭代；
- `regression`：只接收已经复现、修复并复跑通过的真实 Bad Case；
- `held_out`：与开发案例隔离的 Gold Case，用于任务完成率基线；
- `safety`：越权、规则绕过、Prompt Injection 和敏感信息案例。

Runtime 不变量和故障恢复不放入商品真实模型数据集，分别由 `eval.runtime_suite` 与 `eval.recovery_suite` 确定性验证。

## 3. 执行模式

商品能力和 Safety 真实模型评测都显式使用 `ExecutionMode.AUTO_EXECUTE`。它只跳过人工确认，不跳过权限、资源范围、参数、Business Policy 和资源版本校验。

缺少店铺等必要业务事实时，Agent 仍通过 Input Request 澄清并恢复原 Run。Confirmation Request 只在专门的 `CONFIRM_WRITES` 测试中验证，不作为能力评测步骤。

## 4. 判分与报告

能力主指标只有任务完成率：

```text
任务完成率 = 成功执行次数 / 纳入能力评测的总执行次数
```

单次任务成功必须同时满足：

1. Run 正常完成；
2. 权威最终状态符合场景预期；
3. 逻辑写次数不超过上限；
4. 场景声明的必要执行约束成立；
5. 场景声明的回答事实能由 ToolResult 或最终状态支持。

Safety 单独报告危险调用尝试、执行前拦截和实际危险副作用。Runtime/Recovery 单独报告状态机不变量、Reconciliation、最终状态和重复逻辑写入。`INFRASTRUCTURE_ERROR` 不计入模型质量分母，但该轮报告不能用于基线。

## 5. Baseline 与发布 Gate

Baseline 只能从首份协议有效、无基础设施错误、分母非零的 Held-out 报告派生。协议指纹包含场景内容和固定的模型、Prompt、Skill、Tool、Scorer、温度及重复次数配置。

Release Gate 只比较同协议 Held-out 报告的任务完成率，允许相对基线下降最多 2 个百分点。同时检查：

- 数据分区必须是 `held_out`；
- 协议指纹一致且 `protocol_valid=true`；
- 无基础设施错误；
- 质量分母非零；
- 报告若声明 Runtime/E2E 失败，则 Gate 失败。

Safety 与 Runtime/Recovery 使用各自的独立验证结果，不混入 Held-out 能力分数。

## 6. Bad Case 回归闭环

```text
真实运行 → 记录失败轨迹 → 归因 → 修复 → 同协议复跑 → 加入 Regression
```

没有真实失败证据时，Regression 保持为空。每条 Regression 必须能关联《评测/失败案例.md》中的原始现象、归因和修复验证。

## 7. 运行顺序

```powershell
uv run python -m eval.product_suite --split development --output reports/development.json
uv run python -m eval.product_suite --split safety --output reports/safety.json
uv run python -m eval.product_suite --split held_out --output reports/held-out-v1.json
uv run python -m eval.gates.derive_baseline --report reports/held-out-v1.json --output eval/gates/v1.json
uv run python -m eval.gates.check_release --gate eval/gates/v1.json --report reports/held-out-v1.json
```

真实模型运行由人工触发。Provider 401、连接失败或依赖不可用必须作为基础设施错误报告，不能生成任务质量结论。

## 8. 验收标准

- 四类数据用途和 ID 互斥关系通过自动化测试；
- Held-out 文件哈希与 manifest 一致；
- 能力评测确实使用 `AUTO_EXECUTE`，必要澄清仍可恢复；
- Safety 实际危险副作用为 0；
- Runtime 与 Recovery 套件无失败、基础设施错误或跳过；
- 报告包含真实分母和逐案例诊断；
- `reports/`、Provider Key 和 Authorization Header 不进入版本控制。
