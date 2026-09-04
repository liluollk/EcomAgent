# OceanBreeze 电商运营 Agent — 展示版

本目录是独立可跑的前端展示版，**无需启动后端**即可完整交互（所有接口数据由内置 mock 层提供）。适合演示、评审、或离线体验。

## 快速开始

```bash
# 进入展示版目录
cd demo/frontend

# 安装依赖（首次运行）
npm install

# 启动展示版（默认 mock 模式，不需要后端）
npm run dev
```

打开 `http://localhost:5173` 即可体验完整功能：对话流、工具调用卡片、权限确认卡、设置页（渠道/模型 CRUD）、工作台看板等。

## 运行模式

展示版支持两种运行模式，由环境变量 `VITE_OBS_MOCK` 控制：

| 模式 | VITE_OBS_MOCK | 说明 |
|------|---------------|------|
| **Mock 独立（默认）** | `1`（或未设置） | 前端内置假数据 + mock 事件流，无需任何后端，全部接口在该层模拟 |
| **连接真实后端** | `0` | 前端连接项目根后端的真实 HTTP/WS 接口（需要后端已启动） |

### 连接真实后端

```bash
# 1. 启动项目根后端（mock 模式或真实 LLM 皆可）
cd ../..
AGENT_BACKEND=mock python -m uvicorn transport.server:app --port 8000

# 2. 在新的终端中启动展示版（连接后端模式）
cd demo/frontend
VITE_OBS_MOCK=0 npm run dev
```

此时展示版前端通过 Vite 开发服务器将 `/sessions`、`/ws` 等请求转发到后端 `:8000`，体验真实 HTTP/WebSocket 交互。

## 构建产物

```bash
npm run build
```

产物输出到 `demo/frontend/dist/`，可用 `npm run preview` 或任意静态服务器托管。

## 与可交付版的关系

- **展示版（`demo/frontend/`）**：冻结的独立展示前端，内置 mock 数据层，可独立运行。UI 与可交付版一致。
- **可交付版（`frontend/`）**：连接后端真实 API 的前端，无 mock 数据，用于真实部署。
- **后端（`transport/` 等）**：完整 REST/WS 接口，与展示版的 mock 接口形状一致（切换 `VITE_OBS_MOCK=0` 后展示版即可对接后端实际接口）。