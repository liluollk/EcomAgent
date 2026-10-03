# Agent Runtime & Workbench

可持久执行、安全可控、可恢复且可评测的 Agent Runtime,以及其上的 Agent 工作台与商品运营参考 Agent。

当前进度:阶段 1(工程基础与持久化)已完成。Runtime 提供公共类型、PostgreSQL 持久化(41 张表)、Workspace 隔离的 Repository 与事务所有权、本地身份与授权、Secret 加密与外部端点防护、只追加审计。后续阶段(Runtime 执行内核、商品运营参考 Agent、API 工作台、Eval)按 `docs/计划/` 路线图推进。

## 技术栈

Python 3.12、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 16 + pgvector、pytest、Ruff、mypy(strict)。

## 已验证的本地命令

以下命令均在本仓库实际验证通过(Windows + Git Bash):

```bash
uv sync --all-extras          # 安装依赖(自动创建 .venv)
docker compose up -d postgres # 启动 PostgreSQL 16 + pgvector(localhost:5432)
uv run alembic upgrade head   # 建表(空库与升版本均可)
uv run pytest -q              # 全量测试(含 PostgreSQL 集成测试)
uv run ruff check runtime agents adapters apps eval tests
uv run mypy runtime
```

前置条件:Python 3.12、uv、Docker。首次启动容器后等待健康检查通过再执行 migration。

复制 `.env.example` 为 `.env` 可覆盖默认连接串与密钥变量;测试库 `agent_runtime_test` 由 pytest 自动创建和管理。

## 目录结构

- `runtime/` — 公共接口、持久化、访问控制、安全、审计(禁止反向依赖上层模块,有架构测试防护)
- `agents/`、`adapters/`、`apps/`、`eval/` — 后续阶段的业务与产品层(当前为空包)
- `migrations/` — Alembic 迁移
- `docs/` — 需求、架构与实施计划文档
- `tests/` — 单元、持久化与架构测试

## 架构文档

见 `docs/架构/` 与 `docs/计划/`,其中 `docs/计划/00-实施总路线图.md` 定义阶段顺序与各阶段 Gate。
