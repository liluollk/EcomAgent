# Agent Runtime & Workbench

可持久执行、安全可控、可恢复且可评测的 Agent Runtime,以及其上的 Agent 工作台与商品运营参考 Agent。

当前进度:`docs/计划/00-实施总路线图.md` 定义的五个阶段(Runtime 内核、商品 Agent、API 工作台、确定性 Eval、CI)已实现并通过各自 Gate 的本地验证;商品运营模块重组(ADR-002 P0/P1)已实施并提交。DeepSeek 真实模型评测于 2026-10-22 按协议 v3 全量复跑,结果保存在 `eval/gates/v3.json`:Core 96 次执行任务完成率 93.75%(90/96,口径为权威状态 + 过程 + 答复依据 + 行为合同)、状态完成率 94.79%(91/96)、稳定通过 28/32;Safety 17 次危险尝试全部拦截且零副作用;Regression 6/6;Blind 25/30。2026-10-25 协议升到 v4(澄清判分、答复原文进报告、金额接受 JSON 数字、门禁冻结系统指纹),Clarification 分区从 0% 提升到 83.33%(15/18);但 Core/Blind/Safety/Regression 的 v4 复跑因 DeepSeek 额度耗尽中断,**尚未派生 v4 门禁**,因此 v3 的数字只代表旧试卷。判什么、不判什么见 `docs/评测/评测契约.md`,v3 结果见 `docs/评测/复测报告-v3-20261022.md`,v4 现状见 `docs/评测/复测报告-v4-20261025.md`。

## 技术栈

Python 3.12、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 16 + pgvector、FastAPI、uvicorn;React 19 + Vite + Vitest + Playwright(工作台);pytest、Ruff、mypy(strict)、uv。

## 已实现并验证的能力

以下能力均有对应的测试或 Eval 证据(见"已验证的本地命令"),不包含未验证的声明:

- **工程基础与持久化**:公共接口与类型、PostgreSQL 持久化(40 张表)、Workspace 隔离的 Repository 与事务所有权、本地身份与授权、Secret 加密与外部端点防护、只追加审计;
- **Runtime 执行内核**:Backend 流式归一化(工具增量不直接执行)、固定 PreToolUse/PostToolUse 顺序与写前确认(TOCTOU 重校验)、Session 隔离、Context 预算与 Summary(受保护事实保留)、记忆生命周期(删除后不再进入上下文)、幂等与 Reconciliation(UNKNOWN 先回查)、单执行器 state_version CAS 与进程重启恢复、事件重放去重、限额终止、取消后永不恢复——以上 10 条不变式由确定性 Runtime Eval 全部验证通过;
- **商品运营参考 Agent**:按 catalog/pricing/inventory/listing 四个能力模块组织,提供搜索、比价、改价、库存读取、库存调整、Listing 读取、发布、取消发布等商品工具;每条写路径覆盖缺店铺 Input 澄清、写前确认(确认后重校验权限/策略/资源版本)、Mock 平台权威落库、UNKNOWN 回查恢复与同 operation_id 幂等重放;四写工具统一安全矩阵(无写权限、跨店铺、空资源范围、确认后版本变化、响应丢失、重复提交、Adapter 能力边界)由确定性测试验证;能力版本经 Provisioner 发布,Agent/ Skill/Tool/Policy 以 stable key + version + 内容 hash 冻结进 Eval 能力清单。提供固定商品 REST Adapter(仅声明定价能力,其余失败闭合)并以 `httpx.MockTransport` 验证幂等键、版本冲突和不确定写回查。未接入淘宝/抖音官方 API 或真实店铺;
- **API 与工作台**:FastAPI API(统一错误响应体、请求标识、健康检查)与 React 工作台;Workspace overview 聚合真实成员角色、店铺授权与已启用能力,前端支持切换 Workspace,任务只使用当前 Workspace Agent;Session 固定创建时唯一有效的 AgentVersion,后续 Run 不受绑定变更影响;前端单测覆盖 EventReducer 的重复/乱序/缺口/快照/终态不变量;
- **确定性 Eval 与发布门槛**:Runtime 不变式套件(10 用例)与故障恢复套件(7 故障)确定性可重复;商品 Agent 数据集五个分区共 60 例(core 32 / blind 10 / clarification 6 / regression 2 / safety 10,ID 互斥 + Core/Blind 冻结 SHA-256);八类断言(权威状态、答复事实、禁止披露、顺序、工具入参、禁止调用、逐轮答复、澄清顺序)分别判分,分项不折抵、无综合总分、真实分母,决策/安全/恢复/合同/澄清各自独立指标;JSON/Markdown 报告与发布 Gate(`eval/gates/`:缺数据一律 fail closed、安全三项独立硬门槛、Core 无安全用例时必须附 Safety 报告、逐例回归与已知失败频次都不被平均分抵消)。评测契约、协议与历次结果见 `docs/评测/`;
- **CI**:`.github/workflows/` 三个 workflow——backend(PR:postgres/pgvector service + Alembic + Ruff + mypy + pytest)、workbench(PR:lint + Vitest + build,另有完整 E2E job:postgres service + 后端 + Playwright chromium)、eval(PR:确定性 Eval + 数据集校验;真实模型评测仅 `workflow_dispatch` 手动触发,使用仓库 Secrets,不在 PR 中消耗额度)。

## 已验证的本地命令

以下命令均在本仓库实际验证通过(Windows + Git Bash,退出 0):

```bash
uv sync --all-extras          # 安装依赖(自动创建 .venv)
docker compose up -d postgres api  # 生产入口;需先设 RUNTIME_JWT_SECRET 与 RUNTIME_MASTER_KEY
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d postgres api
                              # 演示入口(脚本模型 + 种子接口),无真实模型依赖
uv run alembic upgrade head   # 建表(空库与升版本均可)
uv run ruff check runtime agents adapters apps eval tests
uv run mypy runtime agents adapters composition apps eval          # 248 个源文件
uv run pytest -q              # 1208 passed(需本地 PostgreSQL)
uv run python -m eval.runtime_suite --dataset eval/datasets/runtime
                              # failed=0 infrastructure_errors=0 skipped=0(10 用例)
uv run python -m eval.recovery_suite --dataset eval/datasets/recovery/faults.yaml
                              # actual_duplicate_writes=0,7 故障全过
npm --prefix apps/workbench run lint
npm --prefix apps/workbench run test -- --run          # 49 passed
npm --prefix apps/workbench run build
npm --prefix apps/workbench run test:e2e              # 2 passed
```

前置条件:Python 3.12、uv、Node 24、Docker。首次启动容器后等待健康检查通过再执行 migration。复制 `.env.example` 为 `.env` 并填写 `RUNTIME_JWT_SECRET`、`RUNTIME_MASTER_KEY`(缺失时进程启动即失败,不存在默认密钥);测试库连接串与清库授权见 `docs/开发/本地环境.md`。

## 目录结构

- `runtime/` — 公共接口、持久化、访问控制、安全、审计(禁止反向依赖上层模块,有架构测试防护)
- `agents/`、`adapters/` — 商品运营参考 Agent 与后端/平台适配
- `composition/` — 商品运营 Agent、Runtime、Adapter 与集成服务的产品组合根
- `apps/` — API 与进程内 Run 执行宿主(`apps/api`)、装配(`apps/assembly.py`)、E2E 脚本装配(`apps/scripted_e2e.py`)与工作台(`apps/workbench`)
- `eval/` — Eval Schema、Runner、Scorer、数据集、报告与发布 Gate
- `migrations/` — Alembic 迁移
- `docs/` — 需求、架构、实施计划与评测文档(`docs/评测/`)
- `tests/` — 单元、持久化、架构与 Eval 测试

## 架构与评测文档

架构见 `docs/架构/`,计划见 `docs/计划/`(其中 `docs/计划/00-实施总路线图.md` 定义阶段顺序与各阶段 Gate)。评测判什么见 `docs/评测/评测契约.md`,协议与命令见 `docs/评测/评测方法.md`,各轮结果见 `docs/评测/复测报告-v4-20261025.md`、`docs/评测/复测报告-v3-20261022.md` 与 `docs/评测/首版报告.md`。
