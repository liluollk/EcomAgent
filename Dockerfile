# Agent Runtime API/Worker 镜像(阶段 4 Gate;docker compose 的 api/worker 服务)。
#
# 基于 uv 官方 python3.12-slim 镜像:uv sync --frozen 安装锁定依赖并装好
# 本项目(runtime/agents/adapters/apps/eval 为 wheel 包),容器内直接使用
# .venv 的可执行文件(alembic/uvicorn/python),不再经 uv 派生进程,
# 便于信号(SIGTERM)直达 Worker 实现优雅停止。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 先只复制依赖清单以利用层缓存;安装本项目本身需要全部包源码。
COPY pyproject.toml uv.lock README.md ./
COPY runtime ./runtime
COPY agents ./agents
COPY adapters ./adapters
COPY apps ./apps
COPY composition ./composition
COPY eval ./eval
RUN uv sync --frozen --no-dev

# 迁移脚本与 alembic 配置(api 服务启动时执行 upgrade head)。
COPY alembic.ini ./
COPY migrations ./migrations

EXPOSE 8000
