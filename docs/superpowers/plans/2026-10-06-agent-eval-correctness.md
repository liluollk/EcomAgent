# Agent Eval 正确性修正实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正商品 Agent Eval，使结果、过程和回答依据都被真实执行并按正确分母判分。

**Architecture:** 保留现有 Scenario → EvalRunner → Scorer → Report 架构。先扩充场景契约与结果捕获，再让 Runner 执行真实会话输入/决策，最后增加确定性过程与回答断言、修复分母并更新数据集和文档。

**Tech Stack:** Python、Pydantic、SQLAlchemy、Pytest、YAML、PostgreSQL/SQLite Eval fixture。

---

## 文件边界

- 场景契约与指纹：`eval/schema.py`、`eval/dataset_loader.py`。
- 隔离授权和业务种子：`eval/fixtures.py`。
- 会话输入、确认决定、Run 状态与轨迹捕获：`eval/runner.py`、`eval/results.py`。
- 结果、步骤、安全和证据评分：`eval/scorers/`、`eval/scorers/composite_result.py`。
- 正确分母与可解释报告：`eval/reporting/json_report.py`、`eval/reporting/markdown_report.py`。
- 场景、manifest、方法说明：`eval/datasets/product_operations/*.yaml`、`manifest.json`、`docs/评测/评测方法.md`。
- 对应测试：`tests/eval/test_schema.py`、`test_runner_isolation.py`、`test_scorers.py`、`test_reporting.py`、`test_dataset_partitions.py`。

## Task 1：场景契约、消息指纹和数据校验

- [ ] 为场景增加显式执行者权限/店铺范围、Input 答案、确认决定、必要/禁止过程步骤和回答事实断言。
- [ ] 指纹纳入完整消息与新增场景字段；升级 `SCHEMA_VERSION`，同步 manifest/schema 版本。
- [ ] 用测试证明消息文本、授权、用户决定或判分断言变化都会改变指纹；非法/含糊的决策配置被拒绝。
- [ ] 运行 `uv run pytest tests/eval/test_schema.py tests/eval/test_dataset_partitions.py -q`。

## Task 2：Runner 忠实执行场景并正确表示失败

- [ ] 先添加 Runner 回归测试：后续用户轮次进入同一 Session；Input 使用明确答案；确认只按场景决定接受或拒绝；缺少答案/决定不得自动选择/确认。
- [ ] 在 Runtime 的既有 SessionCommandService 路径上实现 Eval 多轮驱动，不新增仅供测试使用的 Runtime 公共方法。
- [ ] Fixture 按场景授权种子权限和店铺范围，默认拒绝。
- [ ] FAILED/CANCELLED Run 作为任务失败结果保留其真实终态；只有明确基础设施异常单独分类。
- [ ] 运行 `uv run pytest tests/eval/test_runner_isolation.py -q`。

## Task 3：结果、过程和回答证据评分

- [ ] 添加权威最终状态的正反例，确保任务分只由最终状态与副作用约束决定。
- [ ] 添加工具调用目标、必要步骤顺序、禁止调用、授权拦截和确认拒绝的过程评分测试。
- [ ] 捕获最终 Assistant 可见回复及 ToolResult/业务证据；为场景关键回答事实实现确定性一致性评分，不引入 LLM Judge。
- [ ] 添加回答正确、有依据、无依据或与最终状态矛盾的测试。
- [ ] 运行 `uv run pytest tests/eval/test_scorers.py tests/eval/test_runner_isolation.py -q`。

## Task 4：指标分母、失败处理和报告

- [ ] 误拒分母只包含实际出现且命中合法写期望的决策；恢复分母只包含声明恢复目标或出现 UNKNOWN 的适用样本。
- [ ] 任务失败计入任务完成率失败，不把 Agent/Runtime 失败作为缺失样本；基础设施错误只从模型质量结果中排除，并单独展示计数。
- [ ] 报告增加每项指标的分子、分母和不适用数；空分母返回 null。
- [ ] 更新 JSON/Markdown 报告断言与 release gate schema/消费者（若字段变化影响它们）。
- [ ] 运行 `uv run pytest tests/eval/test_reporting.py tests/eval/test_release_gate.py -q`。

## Task 5：修订数据集与方法说明并全量验证

- [ ] 为现有场景补齐执行输入、用户决定、实际权限、步骤约束和关键回答事实；移除或重构未被真实执行的 assistant 伪对话。
- [ ] 加入无写权限、无目标店铺权限、确认拒绝/撤回、低于成本价、事实错误和非法步骤场景；Fixture 提供非零成本价数据。
- [ ] 重新计算冻结集哈希，验证三个 partition 无重复 ID、场景可解析、至少覆盖核心维度。
- [ ] 更新 `docs/评测/评测方法.md`，严格区分 Agent Eval、Runtime 确定性测试和基础设施错误。
- [ ] 运行 `uv run pytest tests/eval -q` 与 `git diff --check`；检查无 API Key/模型调用。
