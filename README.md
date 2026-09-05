# 电商运营 Agent Runtime

面向多品牌女装电商运营场景的通用 Agent Runtime。它将「任务输入 → 上下文装配 → 模型决策 → 工具调用 → 权限确认 → 结果返回」抽象为统一执行闭环，供不同的经营 Agent / Skill 复用同一套执行底座（库存查询、订单分析、促销配置、上下架、售后工单等任务统一在此运行）。

核心链路由 **308 个单元与集成测试**覆盖（全部离线运行，含 E2E 全链路演示脚本），并由**离线评测 Harness**（`harness/`）以行为契约场景 + baseline 指纹做验收对比。

## 两份交付物

| 交付物 | 位置 | 说明 |
|---|---|---|
| **可交付版（真实前后端）** | 项目根 | `frontend/` 接后端真实 REST/WS，后端按「配置驱动」对接渠道平台：填了 base_url + 密钥即发 HTTP（当前预置渠道指向本地模拟网关，真实平台适配层为 TODO），未配置则明确报错，不静默返回假数据 |
| **展示版（mock 前端 + mock 后端）** | [`demo/`](demo/README.md) | 独立可跑的 mock 前端（内置假数据 + mock 事件流，无需后端即可完整交互）；`VITE_OBS_MOCK=0` 时切回连接真实后端 |

两份前端的 UI 与交互一致，接口形状 1:1（mock 层按后端真实响应结构建模），切换只改一个环境变量。

## 特性

- **多模型后端抽象**：OpenAI 兼容 / Anthropic / Mock 统一 `AgentBackend` 契约，流式 Tool Calling 增量解析与跨后端消息归一化在 adapter 内消化，运行时与 UI 不感知具体 SDK。
- **Workspace / Session 状态隔离**：业务规则、凭据引用、模型状态、执行状态、会话审计由两层运行边界承载，JSONL 增量落盘，支持多会话并行与中断后恢复。
- **工具权限治理**：执行前 PreToolUse 管线 = RBAC 身份门（店长/运营/客服/财务，工具级 ACL）→ 业务规则（成本保护）→ 模式门（READONLY / ASK / EXECUTE），allow / block / prompt 三语义，审计带操作人追责。
- **工具三通道**：内置平台 API 工具（11 个电商语义操作 + save_skill，handler 经 REST 客户端 → 平台 Adapter 直达渠道服务；当前为本地模拟网关，真实平台适配层为 TODO）；MCP 外部工具（官方 MCP SDK 客户端池，stdio / JSON-RPC，用户可经 `data/mcp_servers.json` 或前端 MCP 页（粘贴 JSON，兼容 Claude Desktop / Cursor 配置格式）自行接入想用的 MCP server，内置外部工具模拟演示服务）；专用工具（save_skill 技能创建）。
- **技能体系（SKILL.md）**：技能 = 纯运营知识包（SOP 操作手册 / 平台规则），对齐业界 Agent Skills 格式（frontmatter + markdown 正文）；渐进式披露——菜单常驻系统提示词，`load_skill` 命中才注入正文；内置 10 技能（9 电商 + skill_creator 元技能），用户/agent 可经 save_skill 创建新技能（写操作，经 HITL 人工确认后落盘热加载）。
- **渠道配置化接入**：渠道注册表持久化（`data/channels.json`），设置页可新增 / 修改 / 停用渠道（base_url / 鉴权方式 / API Key），保存后即时生效无需重启；每个渠道独立的 REST client（各自 base_url + 鉴权）。
- **可靠性**：平台错误码语义化、写操作幂等（幂等键 + 重试复用同键，防重复创建）、限流/连接失败重试。
- **流式事件状态机**：八类 AgentEvent 驱动 UI；单轮多工具 asyncio 并发执行，结果按 Tool Call ID 归位；AbortHandler + 后端 abort 双通道中断。
- **上下文压缩**：以「模型上下文窗口 - 13k 安全边际」为阈值自动压缩超长会话，也可由用户手动压缩（WS compact 指令）；早期对话折叠为结构化摘要（目标 / 决策 / 进度 / 已用工具），保留近期原文，占用上下文不膨胀。
- **长期记忆**：Claude Code 式索引记忆——`MEMORY.md` 索引 + 独立记忆文件（user / feedback / project / reference 四类型），自动提取沉淀 + 显式「记住/忘记」指令；新会话注入索引与关键词命中的记忆文件全文，跨会话保留经营决策与偏好。

## 技术栈

Python 3.11 · FastAPI · WebSocket · Pydantic v2 · OpenAI SDK · Anthropic SDK · MCP SDK · httpx · React 18 + TypeScript + Vite + Tailwind · pytest

## 架构

```
前端 (React/TS)
  │  WebSocket 事件流 + 权限/中断控制
  ▼
transport/server.py   FastAPI REST + WS 双通道
  ▼  一次 turn
Workspace/Session ── 隔离上下文 / 权限模式 / 模型状态 / 审计
Skill 知识注入 / Source 装配 ── SKILL.md 渐进式披露（菜单常驻、load_skill 注入正文）、激活渠道前置条件
AgentBackend ────── OpenAI / Anthropic / Mock 统一事件流
多轮 Tool Call ─── 回合循环、结果回传、消息组装
PreToolUse ────── 身份门(RBAC) → 业务规则 → 模式门
AgentEvent ─────── 八类事件，驱动 UI 与状态机
  ▼  工具执行（并发调度，结果按 Tool Call ID 归位）
内置平台 API 工具（11 电商操作 + save_skill） │ MCP 外部工具（子进程, stdio/JSON-RPC, 可配置）
  ▼
REST 客户端 (httpx, 真实 HTTP)
  ▼
渠道平台服务 (本地模拟网关进程, REST 端点)
```

协议层（MCP + HTTP/REST）为真实实现；渠道平台返回的业务数据当前为模拟数据（示例商品域：女装）。

## 目录结构

| 模块 | 职责 |
|---|---|
| `transport/` | FastAPI REST + WebSocket 双通道；lifespan 拉起子进程；权限管线装配 |
| `agent_backend/` | 多模型后端抽象（OpenAI / Anthropic / Mock）与消息归一化 |
| `agent_runtime/` | 执行核心：回合循环、工具并发执行、权限挂起、消息组装、上下文压缩与长期记忆 |
| `session/` | Workspace / Session 状态、JSONL 持久化、中断恢复 |
| `permission/` | 权限治理：RBAC 身份门、模式门、业务规则、审计 |
| `sources/` | 工具与能力层：内置平台 API 工具（builtin_tools）、MCP 客户端池 / server 配置、技能注册表（SKILL.md 文件制）、REST 客户端、mock 平台服务、凭据存储 |
| `events/` | 八类 AgentEvent 事件模型 |
| `harness/` | 离线评测 Harness：行为契约场景集、运行器、轨迹记录、指标与 baseline 验收对比 |
| `frontend/` | 可交付版 React + TS 控制台（对话流、工具卡、权限卡），接后端真实 API |
| `demo/` | 展示版：独立可跑的 mock 前端 + 配套说明；无需后端即可完整交互 |
| `tests/` | 308 个测试，按模块分目录；含 E2E 全链路（`tests/e2e/`）与 harness 自身单测（`tests/harness/`） |

## 快速开始

```bash
# 依赖
pip install -e ".[dev]"

# 测试（308 个，全部离线）
python -m pytest tests/ -q

# 离线评测 Harness（行为契约场景 + baseline 验收：通过率退化即非零退出码）
python -m harness                       # 跑全部场景，对照基线验收
python -m harness --save-baseline       # 建立/更新回归基线（baseline.json）
python -m harness --case six_step_business_chain   # 只跑指定场景

# 启动（mock 后端：无需 API key，可跑通全链路）
AGENT_BACKEND=mock python -m uvicorn transport.server:app --port 8000
# 打开 http://127.0.0.1:8000/

# 前端构建（dist 已托管于后端；改前端后重建）
cd frontend && npm install && npm run build

# 展示版（demo/）：独立可跑的 mock 前端，无需后端
cd demo/frontend && npm install && npm run dev   # 打开 http://127.0.0.1:5173
```

服务启动时按 `data/mcp_servers.json` 配置拉起 MCP 外部工具子进程与渠道平台服务子进程，停止时自动回收。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_BACKEND` | `openai` | 后端选择：`openai` / `anthropic` / `mock` |
| `AGENT_MODEL` | 按后端 | 模型名 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | — | OpenAI 兼容配置 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | — | Anthropic 配置 |
| `CHANNEL_API_PORT` | `18080` | 渠道平台服务端口 |
| `CHANNEL_API_URL` | `http://127.0.0.1:18080` | REST 客户端目标地址 |
| `CHANNEL_API_AUTO` | `1` | 置 `0` 关闭自动拉起渠道平台服务 |
| `CHANNEL_CONFIG_FILE` | `data/channels.json` | 渠道配置持久化路径（默认即可，跨进程共享） |
| `AGENT_STORAGE_DIR` | `data/sessions` | 会话 JSONL 持久化目录 |
| `MEMORY_DIR` | `data/memory` | 长期记忆目录（MEMORY.md 索引 + 独立记忆文件） |
| `PROVIDER_CONFIG_FILE` | `data/providers.json` | 模型供应商配置持久化路径 |
| `SKILLS_DIR` | `data/skills` | 用户技能目录（SKILL.md 文件制，mtime 热加载） |
| `MCP_SERVERS_CONFIG_FILE` | `data/mcp_servers.json` | MCP server 配置持久化路径（前端 MCP 页可增删改） |

## 管理 API

会话与对话之外的运行时管理接口（设置页调用）：

| 接口 | 说明 |
|---|---|
| `GET /sources` | 渠道列表（敏感字段掩码） |
| `POST /sources` | 新增渠道（name / label / base_url / platform / auth_type / api_key / options） |
| `PATCH /sources/{name}` | 修改渠道配置（含启用 / 停用 / 平台类型），即时生效 |
| `DELETE /sources/{name}` | 删除渠道（内置三渠道不可删） |
| `POST /sources/{name}/test` | 连通性测试（mock 走只读探测；真实平台走 adapter.probe） |
| `GET /providers` | 模型供应商列表（api_key 掩码）+ 当前激活者 |
| `POST /providers` | 新增供应商（openai / anthropic / mock） |
| `PATCH /providers/{name}` | 修改供应商（模型 / API Key / base_url / 启停 / thinking_level 思考强度） |
| `DELETE /providers/{name}` | 删除供应商（内置三供应商不可删） |
| `POST /providers/{name}/activate` | 设为当前激活供应商（新连接即时生效） |
| `POST /providers/{name}/test` | 供应商配置连通性测试 |
| `GET /workspaces` | 工作空间列表（多品牌隔离） |
| `POST /workspaces` | 创建工作空间（workspace_id / name / brand / rules） |
| `GET /workspace/overview` | 工作台聚合数据：各渠道订单 / GMV / 促销 / 异常 / 库存（真实协议层取数，前端总览与促销分区消费） |
| `GET /mcp/status` | MCP 外部工具通道状态：连接状态 + 各 server 工具发现聚合（前端 MCP 页消费） |
| `GET /mcp/servers` | MCP server 配置列表（env 值掩码） |
| `POST /mcp/servers` | 新增 MCP server（name / command / args / env），保存后重连生效 |
| `PATCH /mcp/servers/{name}` | 修改 MCP server 配置（含启用 / 停用） |
| `DELETE /mcp/servers/{name}` | 删除 MCP server 配置 |
| `POST /mcp/servers/{name}/test` | MCP server 连通性测试（拉起子进程 initialize 握手 + 工具发现） |
| `GET /skills` | 技能列表（SKILL.md 知识包：内置只读 + 用户技能） |
| `POST /skills` | 新增用户技能（name / description / keywords / body） |
| `PATCH /skills/{name}` | 更新 / 启停用户技能（内置技能只读） |
| `DELETE /skills/{name}` | 删除用户技能（内置技能不可删） |
| `GET /sessions/{id}/mode` | 会话权限模式 |
| `PUT /sessions/{id}/mode` | 切换会话权限模式（持久化 + 广播 mode_change） |

新增渠道无需新增工具：11 个内置电商工具按 `channel` 参数分派，渠道配置写入后（主进程与渠道注册表共享同一配置文件）下次调用即生效。

## 接入真实渠道

**已配置化的部分（设置里可操作，无需改代码）**：
- 协议层（MCP + HTTP/REST）真实；添加渠道 = 填 `base_url` + 鉴权配置（mock / API Key），保存后即发 HTTP 到目标地址（当前预置渠道指向本地模拟网关），运行时、前端、权限、审计均无需改动。
- 管理 API 与设置页已接通：有平台资质后，可在设置页新增真实平台渠道、填密钥并保存；但各真实平台（淘宝 TOP / 京东 JOS / 抖音）的字段映射与签名实现尚未落地（见下），当前业务实际运行于本地模拟网关。

**仍属每渠道 adapter 的部分（真实多平台系统的固有成本）**：
- 各平台（淘宝 TOP / 京东 JOS / 抖音）开放 API 的字段映射、签名算法与错误码语义不同，需为每个真实平台各写一份 handler 适配层；本骨架不代写，但核心执行底座复用。

平台资质可后续申请：读写接口按各开放平台开发者资质与企业应用审核放行；资质到位且对应 adapter 的签名 / 字段映射实现后，方可逐步接入真实平台，无需重写运行时。

## 项目状态（当前交付）

### 能力清单

- **执行闭环**：Workspace/Session → Skill/Source 装配 → AgentBackend（OpenAI/Anthropic/Mock 统一契约）→ 多轮工具并发执行 → PreToolUse 权限管线 → AgentEvent 流式状态机。
- **权限治理**：RBAC 四角色 + READONLY/ASK/EXECUTE 模式 + 业务规则（成本保护），审计带操作人。
- **工具三通道**：内置平台 API 工具（REST 真实 HTTP → Adapter）；MCP 外部工具（stdio/JSON-RPC，server 配置前端可增删改 + 测连通，支持粘贴 JSON 接入）；专用工具（save_skill 经 HITL 创建技能）。渠道注册表配置化，设置页可新增 / 启停 / 测连通，保存即时生效。
- **可靠性**：幂等键 + 限流重试 + 平台错误码语义化 + 会话 JSONL 持久化与恢复。
- **上下文压缩**：模型窗口 - 13k 阈值自动压缩 + 手动压缩，早期对话折叠为结构化摘要。
- **长期记忆**：MEMORY.md 索引 + 独立记忆文件（user/feedback/project/reference 四类型），自动提取 + 显式记住/忘记。

### 验证

- 308 个测试全离线通过（含 E2E 全链路脚本 `tests/e2e/test_e2e_demo.py`）；
- E2E 已验证：六步业务演示链路、动态渠道新增 → 对话即时调用、幂等回放、成本拦截、跨会话记忆沉淀、技能创建（save_skill 落盘 + 热加载进菜单）；
- 离线评测 Harness 验收：`python -m harness` 以行为契约场景驱动完整链路（内置工具 + 真实 MCP 子进程 + 剧本后端），
  事件流 trace 落盘 `.harness-runs/{run_id}/`；与 `baseline.json` 场景指纹 + 通过率对比，
  评测集被改动或通过率低于基线时以非零退出码结束（行为退化拦截）。

### 评测体系（四层配方）

- **L1 单元契约**：各模块功能正确性（sources 87 / transport 52 / agent_runtime 50 / permission 34 / agent_backend 24 / session 23 / harness 17 / events 14）；
- **L3 行为契约场景**：评测集为独立数据表（`harness/cases.py`，单一事实源，pytest 与 harness 共用）——输入 → 期望轨迹（工具序列/参数/失败语义）统一按表断言，新增场景 = 加一行数据、不改断言逻辑；当前 5 场景（六步链路 / 成本拦截 / 动态渠道 / 跨会话记忆 / 技能创建）；
- **L1 对抗场景**：`test_tool_loop_adversarial.py` 覆盖执行循环边界——多工具并发、乱序结果按 ID 匹配、局部失败隔离、缺失参数转 is_error；
- **L4 回归基线**：全量通过率 + 结果存档，作为每次改动的退化判定基线；
- **L2 协议契约**（挂起）：平台 API 响应信封/签名/错误码的 MockTransport 契约测试，待真实平台适配器（P0）落地后同步实现。

### 已知边界（诚实声明）

1. 业务数据为模拟：协议层（MCP / REST）真实，Mock 的只是平台返回数据；接真实平台需平台资质 + 各平台字段/签名适配层（真实多平台系统的固有成本）。
2. 摘要与记忆检索为规则 / 关键词实现：LLM 摘要接口与向量检索升级路径已预留。
3. 多品牌 workspace 隔离已实施（`/workspaces` 端点、会话按 workspace 绑定、记忆按 workspace 隔离）。


### 后续可选路线

- 前端手动压缩按钮（后端 WS compact 通道已就绪）；
- 真实渠道接入（先接只读 API + 沙箱）；
- 记忆检索升级 embedding、摘要接真实 LLM。
