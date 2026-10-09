# Eval 与发布门槛实施方案

## 1. 目标

用固定 Core 验证商品 Agent 的任务完成能力，用独立 Safety 验证写安全，用确定性套件验证 Runtime 与 Recovery。结果分开报告，不合成综合分数。

## 2. 数据结构

- `core`：30 条固定 Gold Case，产生可比较的任务完成率；
- `blind`：10 条优化期间不运行的盲测任务；
- `clarification`：6 条缺参追问与恢复任务；
- `regression`：真实 Bad Case 修复后的回归任务；
- `safety`：8 条越权、规则绕过和敏感信息任务。

Core 与 Blind 使用独立哈希冻结。Runtime 不变量和故障恢复分别由 `eval.runtime_suite` 与 `eval.recovery_suite` 验证。

## 3. 主指标

```text
任务完成率 = Core 成功执行次数 / 90
稳定通过率 = 连续 3 次全成功的 Core 任务数 / 30
```

任务成功由 Run 状态、权威最终状态、逻辑写入次数、必要过程约束和回答证据共同决定。重复运行衡量稳定性，不当作新的独立任务。

## 4. Baseline 与 Gate

Baseline 只能从协议有效、无基础设施错误、分母非零的 `core` 报告派生。Release Gate 只比较相同协议指纹的 Core 任务完成率，允许相对基线下降最多 2 个百分点，同时检查分区、协议、基础设施错误和非空分母。

Safety、Clarification、Runtime 和 Recovery 不混入 Gate 的能力分数。

## 5. 运行顺序

```powershell
uv run python -m eval.product_suite --split core --output reports/core-baseline.json
uv run python -m eval.product_suite --split clarification --output reports/clarification.json
uv run python -m eval.product_suite --split regression --output reports/regression.json
uv run python -m eval.product_suite --split safety --output reports/safety.json

# Core 优化结束后才运行
uv run python -m eval.product_suite --split blind --output reports/blind.json
```

Provider 401、连接失败或依赖不可用作为基础设施错误处理，不能生成任务质量结论。

## 6. 验收标准

- 五个分区 ID 互斥并与 Manifest 一致；
- Core=30、Blind=10、Clarification=6；
- Core 六类任务数量为 `4/6/6/6/4/4`；
- Core 与 Blind 哈希运行前校验；
- 报告同时包含执行级完成率和场景级稳定通过数；
- Safety 实际危险副作用为 0；
- Runtime 与 Recovery 套件通过且无重复逻辑写入；
- `reports/` 和 Provider Key 不进入版本控制。
