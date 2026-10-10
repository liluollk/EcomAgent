# Agent 核心评测集实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 30 条核心 Gold Case、10 条 Blind Case 和独立 Clarification 分区，并运行真实模型 Baseline。

**Architecture:** Eval 继续通过商品业务装配调用 Runtime，但对外只保留 `core`、`blind`、`clarification`、`regression`、`safety` 五类商品数据。Manifest 固定分区 ID 和 Core/Blind 文件哈希；报告在执行级任务完成率之外计算场景连续 3 次稳定通过数。

**Tech Stack:** Python 3.12、Pydantic、PyYAML、pytest、DeepSeek OpenAI-compatible API、PostgreSQL。

---

### Task 1: 冻结分区契约

**Files:**
- Modify: `tests/eval/test_dataset_partitions.py`
- Modify: `tests/eval/test_reporting.py`
- Modify: `eval/dataset_loader.py`
- Modify: `eval/product_suite.py`
- Modify: `eval/gates/derive_baseline.py`
- Modify: `eval/gates/check_release.py`

- [ ] **Step 1: 先写失败测试**

测试必须断言五个分区名称、Core/Blind 哈希校验、Core 30 条、Blind 10 条、Clarification 6 条，以及 Release Gate 只接受 `core` 报告。

- [ ] **Step 2: 验证测试因旧四分区契约失败**

Run: `uv run pytest tests/eval/test_dataset_partitions.py tests/eval/test_reporting.py tests/eval/test_release_gate.py -q`

Expected: FAIL，错误指向缺少 `core_ids`、`blind_ids`、`clarification_ids` 或未知 split。

- [ ] **Step 3: 实现最小分区契约**

`DatasetManifest` 使用以下字段：

```python
core_ids: tuple[str, ...]
blind_ids: tuple[str, ...]
clarification_ids: tuple[str, ...]
regression_ids: tuple[str, ...]
safety_ids: tuple[str, ...]
core_sha256: str
blind_sha256: str
```

`product_suite` 的 `_SPLIT_FILES` 固定为：

```python
{
    "core": "core.yaml",
    "blind": "blind.yaml",
    "clarification": "clarification.yaml",
    "regression": "regression.yaml",
    "safety": "safety.yaml",
}
```

Core 与 Blind 运行前分别校验文件字节 SHA-256；Release Gate 只接受 `dataset.split == "core"`。

- [ ] **Step 4: 验证分区契约测试通过**

Run: `uv run pytest tests/eval/test_dataset_partitions.py tests/eval/test_reporting.py tests/eval/test_release_gate.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add eval/dataset_loader.py eval/product_suite.py eval/gates/derive_baseline.py eval/gates/check_release.py tests/eval/test_dataset_partitions.py tests/eval/test_reporting.py tests/eval/test_release_gate.py
git commit -m "refactor: define core eval partitions"
```

### Task 2: 构建 Core、Blind 和 Clarification 数据

**Files:**
- Create: `eval/datasets/product_operations/core.yaml`
- Create: `eval/datasets/product_operations/blind.yaml`
- Create: `eval/datasets/product_operations/clarification.yaml`
- Modify: `eval/datasets/product_operations/regression.yaml`
- Modify: `eval/datasets/product_operations/manifest.json`
- Delete: `eval/datasets/product_operations/development.yaml`
- Delete: `eval/datasets/product_operations/held_out.yaml`
- Test: `tests/eval/test_dataset_partitions.py`

- [ ] **Step 1: 先增加任务分布测试并验证失败**

Core 类别使用 `read_only_query`、`direct_write`、`relative_change`、`multi_store_decision`、`composite_task`、`multi_turn`，数量必须为 `4/6/6/6/4/4`；Core 不得包含 Interaction，Clarification 每条必须包含 Input Interaction；Core 与 Blind 的 `(store_id, sku)` 必须互斥。

Run: `uv run pytest tests/eval/test_dataset_partitions.py -q`

Expected: FAIL，提示数量、类别或分区文件不符合新契约。

- [ ] **Step 2: 写入固定 Gold Case**

Core 使用 `core-read-*`、`core-write-*`、`core-relative-*`、`core-multistore-*`、`core-composite-*`、`core-multiturn-*` ID；Blind 使用 `blind-*` ID 和独立店铺/SKU；Clarification 使用 `clarify-store-*`、`clarify-sku-*`、`clarify-price-*` ID。所有正常写任务最多一次逻辑写，查询任务零写。

- [ ] **Step 3: 更新 Manifest 与哈希并验证**

Run: `uv run pytest tests/eval/test_dataset_partitions.py -q`

Expected: PASS，Core=30、Blind=10、Clarification=6，哈希匹配。

- [ ] **Step 4: 提交**

```bash
git add eval/datasets/product_operations tests/eval/test_dataset_partitions.py
git commit -m "test: build core agent gold cases"
```

### Task 3: 报告稳定通过数

**Files:**
- Modify: `tests/eval/test_reporting.py`
- Modify: `eval/reporting/json_report.py`
- Modify: `eval/reporting/markdown_report.py`

- [ ] **Step 1: 先写失败测试**

构造两个场景各 3 次结果：一个 `3/3`，一个 `2/3`。断言报告包含：

```python
metrics["scenario_stability"] == {
    "total_scenarios": 2,
    "stable_passed_scenarios": 1,
    "stable_pass_rate": 0.5,
}
```

- [ ] **Step 2: 验证测试失败**

Run: `uv run pytest tests/eval/test_reporting.py -q`

Expected: FAIL，`scenario_stability` 不存在。

- [ ] **Step 3: 实现按 Case ID 聚合**

只聚合 `included_in_quality_score=True` 的执行；同一 Case 的全部执行均通过才计为稳定通过。Markdown 同时显示 `stable_passed_scenarios/total_scenarios`。

- [ ] **Step 4: 验证报告测试通过并提交**

Run: `uv run pytest tests/eval/test_reporting.py -q`

Expected: PASS。

```bash
git add eval/reporting/json_report.py eval/reporting/markdown_report.py tests/eval/test_reporting.py
git commit -m "feat: report scenario stability"
```

### Task 4: 收口中文文档

**Files:**
- Modify: `docs/评测/评测方法.md`
- Modify: `docs/评测/首版报告.md`
- Modify: `docs/评测/失败案例.md`
- Modify: `docs/计划/05-Eval与发布门槛.md`

- [ ] **Step 1: 统一为 Core/Blind/Clarification/Regression/Safety 口径**

删除将澄清混入主完成率、将旧 Held-out 当最终 Baseline，以及 Development/Regression/Frozen Test 三层叙事。保留 2026-10-09 旧结果作为“历史诊断记录”，明确不可与新 Core 协议直接比较。

- [ ] **Step 2: 检查旧口径残留**

Run: `rg -n "Development|Held-out|held_out|Frozen Test|开发集|保留集" docs/评测 docs/计划/05-Eval与发布门槛.md`

Expected: 仅历史诊断说明或无命中，不存在当前运行指令和当前指标口径残留。

- [ ] **Step 3: 提交**

```bash
git add docs/评测 docs/计划/05-Eval与发布门槛.md docs/superpowers/specs/2026-10-09-agent-eval-dataset-redesign.md docs/superpowers/plans/2026-10-09-agent-eval-dataset-redesign.md
git commit -m "docs: define core agent eval protocol"
```

### Task 5: 确定性验证与真实 Baseline

**Files:**
- Preserve untracked: `reports/`
- Modify after verified run: `docs/评测/首版报告.md`
- Modify after verified failures: `docs/评测/失败案例.md`

- [ ] **Step 1: 运行静态与确定性验证**

```bash
uv run pytest tests/eval tests/agents/product_operations tests/runtime/orchestration -q
uv run ruff check eval tests/eval
uv run mypy eval agents
uv run python -m eval.runtime_suite --dataset eval/datasets/runtime
uv run python -m eval.recovery_suite --dataset eval/datasets/recovery/faults.yaml
```

Expected: 全部通过；Recovery 的实际重复逻辑写入为 0。

- [ ] **Step 2: 隐藏注入 Provider 凭据并运行 Core**

```powershell
$env:EVAL_BASE_URL = "https://api.deepseek.com"
$env:EVAL_MODEL = "deepseek-flash"
$secureEvalKey = Read-Host -AsSecureString
$env:EVAL_API_KEY = [System.Net.NetworkCredential]::new('', $secureEvalKey).Password
uv run python -m eval.product_suite --split core --output reports/core-baseline-20261009.json
Remove-Item Env:EVAL_API_KEY
```

执行时将 `SecureString` 仅转换到当前进程环境；不得写入命令历史、文件、报告或 Git。

- [ ] **Step 3: 审计报告**

确认场景数为 30、执行数为 90、Infrastructure Error 为 0，并记录成功次数、任务完成率、稳定通过数和逐类失败。此时不运行 Blind。

- [ ] **Step 4: 记录真实结果并提交文档**

```bash
git add docs/评测/首版报告.md docs/评测/失败案例.md
git commit -m "docs: record core eval baseline"
```
