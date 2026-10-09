# Agent 评测集实施完成记录

## 完成范围

- 商品数据集按 `development`、`regression`、`held_out`、`safety` 四种用途组织；
- 商品能力评测显式使用 `ExecutionMode.AUTO_EXECUTE`；
- 必要业务信息继续通过 Input Request 澄清；
- 任务成功由最终状态、必要执行约束和回答证据共同判定；
- Held-out Baseline 与 Release Gate 只比较任务完成率；
- Safety、Runtime 不变量和 Recovery 分别验证；
- Bad Case 先记录、归因、修复和复跑，之后才能进入 Regression；
- 真实模型报告保存在未跟踪的 `reports/` 目录。

## 验证结果

```text
tests/eval: 126 passed
tests/agents/product_operations: 99 passed
Runtime suite: 10 passed
Recovery suite: 7 passed, actual_duplicate_writes=0
ruff: passed
mypy agents eval: passed
```

## 当前运行口径

权威说明见：

- `docs/评测/评测方法.md`
- `docs/计划/05-Eval与发布门槛.md`
- `docs/评测/失败案例.md`

真实模型评测依次运行 Development、Safety、Held-out。只有当前协议下有效的 Held-out 报告可以派生 Baseline。
