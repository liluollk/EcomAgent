<div align="center">

# EcomAgent

**面向电商运营的 Agent 平台 · 任务编排 / Tool 安全控制 / 多平台调价控制面 / 行为回归**

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688?style=flat&logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?style=flat&logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-Vite-3178C6?style=flat&logo=typescript&logoColor=white)
![pytest](https://img.shields.io/badge/pytest-515%20passed-0A9EDC?style=flat&logo=pytest&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Client%20Channel-8A2BE2?style=flat)

</div>

---

## 目录

- [这是什么](#-这是什么)
- [核心设计](#-核心设计)
- [功能全景](#-功能全景)
- [架构](#-架构)
- [目录结构](#-目录结构)
- [快速开始](#-快速开始)
- [质量与验证](#-质量与验证)
- [边界声明](#-边界声明)

---

## 💡 这是什么

把「任务输入 → 上下文装配 → 模型决策 → 工具调用 → 权限确认 → 执行策略 → 平台写入 → 结果回查」抽象为统一执行闭环。当前的主闭环是**多平台商品调价**：同一个调价领域命令可分别经淘宝与抖店的 Adapter 落到各自平台，跨平台抽象的成立性与操作的可恢复性都有显式契约和测试兜着。

**它不是 Prompt + Tool Calling 的演示，而是把 Agent 当作可持续执行的业务系统来做**——每一层都有明确的契约与测试。

---

## 🎯 核心设计

**① AgentEvent——一份事件流，三个消费方。** 八类事件同时驱动前端 UI、评测断言与轨迹回放，没有第二套日志；可观测字段是增补，不是重新设计。

<details>
<summary>展开细节</summary>

平台对外只暴露八类事件（`text_delta / tool_start / tool_result / permission_request / typed_error / status / complete / abort`）。同一份事件流同时驱动三个消费方，不存在第二套日志或第二套状态。可观测性字段（`trace_id / attempt / duration_ms / idempotent_replay`）是后来在事件上**增补的字段，不是重新设计的事件**：Execution Policy 执行完把元数据回填进 `tool_result`，Trace 就从事件流上自然长出来了。

</details>

**② Tool 安全控制——权限是系统边界，模型无法绕过。** 角色 → 业务规则 → 模式门三层全部由规则引擎判定；ASK 模式下写操作挂起整个 turn 等待人工批准，批准后从挂起点恢复。

<details>
<summary>展开细节</summary>

PreToolUse 管线 = **RBAC 身份门**（店长 / 运营 / 客服 / 财务四角色，工具级 ACL）→ **业务规则**（如成本保护：调价不得低于成本价）→ **模式门**（READONLY 只读 / ASK 询问 / EXECUTE 自动执行）。所有工具都通过显式 `ToolPolicy` 声明读写副作用与审批要求，未知策略默认拒绝，不依赖 `query_` / `get_` 等名称前缀猜测。三类判定全部由规则引擎产出，模型无法绕过——成本保护还是双闸：PreToolUse 早闸拦截后，平台侧再以**自身成本数据**硬拒低于成本的写请求（不信任调用方传入的 cost_price），真实模型漏报或谎报成本也越不过去。ASK 模式下写操作会发 `permission_request` 事件并**挂起整个 turn**（异步 resolver 等待），批准后从挂起点恢复执行，拒绝则把拒绝原因作为工具结果回传给模型重新规划；对话内权限卡与跨会话「审批中心」双入口可响应同一挂起请求，全程留审计（谁批准的、批了什么）。

</details>

**③ Execution Policy——重试是分类驱动的决策，不是「失败就再试一次」。** 错误四分类、仅瞬态错误指数退避、重试复用同一幂等键、读写分级超时、双层结果校验（HTTP 200 ≠ Tool 成功）。

<details>
<summary>展开细节</summary>

「允许执行」和「执行成功」之间是执行可靠性层，收口五件事：

- **错误四分类**：CLIENT_ERROR / BUSINESS_ERROR 不重试，TRANSIENT_ERROR（429 / 503 / 超时）指数退避重试，FATAL 按策略；
- **幂等键**：写操作自动生成 `idem_{session_id}_t{turn}_{tool_name}_{params_hash}`，**重试复用同一把键**，Adapter 经 `X-Idempotency-Key` 头透传，平台侧按键去重——回答「Agent Tool 重试会不会重复副作用」；调价走的是操作级幂等键 `price_{operation_id}`，由操作 ID 派生、**生命周期内不变**，因此重试、审批恢复、未知结果恢复都落在同一个键上；
- **读/写分级超时**：读操作放宽、写操作收紧，超时按分类归入瞬态可重试；
- **双层结果校验**：Schema 校验 + 业务校验——上游返回畸形数据（缺字段 / 类型错）会被拦成显式错误，而不是把脏数据喂给模型；
- **元数据回填**：实际尝试次数、总耗时、是否幂等回放，写回事件供 UI 徽章与 Trace 消费。

</details>

**④ 调价控制面——执行编排不感知「淘宝」。** 上层只见领域层三个动作：查快照 / 提交改价 / 结果回查；淘宝与抖店把各自的协议形态、签名、价格单位与错误码收进自己的 Adapter，平台私有字段不进入领域对象。

<details>
<summary>展开细节</summary>

主闭环是**多平台商品调价**，整条链路固定为：

```
模型 Tool → 调价领域命令 → 能力检查 / 规则校验 / 审批 → 操作状态机 → Taobao·Douyin Adapter → 平台 API → 结果回查
```

**领域契约**在 `integrations/commerce/price_models.py`：`ProductRef`（平台 + 店铺 + 商品 + SKU）、`ProductSnapshot`、`PriceConstraints`、`PriceChangeCommand`、`PriceWriteReceipt`、`PriceVerification`，以及统一错误 `PriceError(code, platform_code, side_effect_possible)`。`PricePlatform` 是执行编排唯一依赖的接缝，只有三个方法：

| 方法 | 语义 |
|---|---|
| `query_snapshot(ref)` | 商品快照：当前价 / 库存 / 状态 / 进行中活动与是否被活动锁价 |
| `apply_price(command, idempotency_key=…)` | 提交一次改价，返回只含领域字段的 `PriceWriteReceipt` |
| `verify_price(ref, expected_price)` | 平台结果回查——改价是否生效以回查为准，不靠写接口的返回码 |

`get_price_platform(platform)` 按平台取实现。**两种明显不同的协议形态收敛到同一份领域命令**：淘宝走 TOP 参数信封（单端点按 `method` 分发、`hmac-sha256` 签名、价格是「元」的两位小数字符串），抖店走 JSON body + `access_token`（价格是「分」的整数）。同一个 `PriceChangeCommand(target_price=Decimal("89.00"))` 经过两个 Adapter，请求形态与价格单位都不同，解析出的领域结果一致；签名、token、app_key 只存在于 Adapter 内部，**不写进任何领域对象或审计记录**，因此不会泄漏给模型。平台差异还有一层是**能力画像**（`capabilities.py`）：平台不支持的能力以 `PriceError(CAPABILITY_UNSUPPORTED)` 显式拒绝，而不是让上层靠猜。

**成本是内部真相，不由模型提供。** 成本价不属于任何 Tool 入参或领域命令，只能经内部 `CostProvider`（`integrations/commerce/cost_provider.py`）从平台侧数据取得；成本缺失或查询异常时高风险调价默认拦截。Workspace 的成本保护开关经 `PATCH /workspaces/{workspace_id}` 规范化后保存到独立 JSON，设置页只负责配置，不构成安全边界。

**边界如实说明**：淘宝与抖店的协议形态、签名、错误码映射，以及两套平台的 Mock 网关（`mock_commerce/`，in-process 运行）都是**离线契约实现**，用来验证跨平台抽象是否真的成立。`jd` / `open` 仍是 stub，`probe()` 诚实返回「尚未接入」。本版本**没有**接入任何真实电商平台的凭证或生产流量，Mock 通过不等于生产可用。

接口面也划清楚：本版本的调价控制面只覆盖**商品调价**。客服工单、知识库、经营统计、上下架这些能力要么属于扩展工具通道（不在默认注册表里），要么只是 Mock 网关提供的测试数据，**都不属于调价控制面的能力主张**；对外描述时不把它们算进这套抽象的覆盖范围。

</details>

**⑤ 可恢复调价操作——写超时不是失败，是一个待确认的状态。** 调价有显式状态机与逐条落盘的审计记录；写入超时进入 `UNKNOWN_OUTCOME` 后靠回查收敛，恢复时复用同一个 `operation_id` 与幂等键。

<details>
<summary>展开细节</summary>

一次调价不是一次函数调用，而是一个有状态、可中断、可恢复的操作。正常路径与异常路径都在同一张状态机里：

```
CREATED → PRECHECKED → WAITING_APPROVAL → EXECUTING → VERIFYING → SUCCEEDED
                ↘ BLOCKED     ↘ REJECTED      ↘ RETRYING
                                              ↘ UNKNOWN_OUTCOME → VERIFYING → SUCCEEDED
```

几个刻意的设计点：

- **非法状态转移被拒绝**，而不是被静默接受——状态机自己守门，不依赖调用方小心；
- **写入超时不判失败**：Adapter 区分「写前超时」与「写后超时」，后者带 `side_effect_possible=True`，协调器据此进入 `UNKNOWN_OUTCOME` 再回查。服务端已生效却被客户端判成失败，是分布式系统里最危险的一类错误；
- **恢复复用同一个 `operation_id` 和同一把幂等键**：审批挂起、执行中断、未知结果三种恢复姿态都从审计记录重建操作，不换键、也不盲目产生第二次写入（Harness 用平台调用计数与副作用条数断言这一点）；
- **审批上下文贯通**：`permission_request` 事件与 `session.permission_requests` 携带 `operation_id`、平台、商品引用、目标价与规则摘要（如成本保护结论）。`operation_id` 由引擎在权限检查**之前**生成并注入工具入参，因此「权限卡上的 ID」与「协调器实际用的 ID」是同一个，不是各算各的；批准后恢复的是**同一个**操作，拒绝则不产生任何平台副作用。

每条操作事件按会话隔离写入 JSONL（`operation_created / state_changed / approval_decided / platform_attempt / verification_observed`），可从记录重建当前状态与完整轨迹——这既是恢复能力的事实来源，也是评测断言共用的读通道。

</details>

**⑥ Mock Commerce API——把第三方边界做成被测系统。** 独立可运行的电商服务模拟，同时提供**淘宝 TOP 信封**与**抖店 JSON** 两套协议形态、共享同一份商品状态，以及按操作语义定向的确定性故障注入。

<details>
<summary>展开细节</summary>

没有平台资质，就把第三方边界做成一个**独立的小型电商服务**（`mock_commerce/`，可独立进程运行）当被测系统来认真做：API Key 认证、`request_id + code + data` 错误码信封、限流、业务冲突、**幂等存储**（键 → 响应，重放不重复记副作用，另留一份写操作审计日志）。

**两套协议形态，一份商品状态。** 淘宝风格走 TOP 参数信封（单端点按 `method` 分发、HMAC-SHA256 签名、价格单位是「元」的两位小数字符串），抖店风格走 JSON body + `access_token`（价格单位是「分」的整数）；两边读写的是同一份商品状态与同一份副作用日志——所以「淘宝改价 → 抖店回查」能看到一致结果，跨平台抽象的正确性也才真的被验证到，而不是两个平台各验自己。成本价只能来自平台侧数据，低于成本的写入一律被平台拒绝。

**幂等键按端点命名空间隔离。** 真实平台的幂等键只在同一个接口内唯一；若网关把全局键当成同一个命名空间，淘宝与抖店用同一个键时后者会收到前者的响应体（协议信封完全不同，直接解析失败）。

最有价值的是**两阶段确定性故障注入**：故障在「副作用落库之后」才让客户端超时——精确复现分布式系统里最危险的「服务端已生效、客户端超时」场景。脚本覆盖调价闭环的关键失败面：`rate_limit_once_then_success`、`write_timeout_before_commit`、`write_timeout_after_commit`、`business_error_permanent`、`capability_refused`、`verify_mismatch`、`verify_timeout`、`verify_timeout_permanent`、`malformed_once_then_success`、`permanent_500`。故障以剧本表达，同一输入永远得到同一轨迹。

</details>

**⑦ 行为验证体系（Evaluation Harness）——用例是数据，不是代码。** 21 条端到端场景全部是商品调价场景，按 `gold / guarded / resilience / recovery` 四类组织；剧本后端 + 确定性故障保证同输入同轨迹；与 baseline 指纹对比，行为退化即非零退出。契约分两档——**决策契约**（模型选哪个工具、传的 SKU 与目标价对不对，剧本专属）与**执行契约**（权限、状态流转、平台调用次数、幂等回放、最终回查、副作用数，模型无关）；换 `--backend openai --provider <providers.json 中的供应商名>` 即进入真实模型评测模式，只跑执行契约子集。

<details>
<summary>展开细节</summary>

场景表是单一事实源：每条场景声明**平台**、输入（消息 / 期望工具 / 参数子集）、工具序列、**期望操作状态**、**重试次数**、**副作用数**与领域指标——新增字段驱动断言，而不是给每条场景写一个专用函数。Runner 驱动完整链路（内置工具 + 真实 MCP 子进程 + 剧本后端 + 故障脚本），逐条断言后与 **baseline 场景指纹对比，通过率退化即非零退出**。它与 pytest 是两个概念：**pytest 是代码回归网，Harness 是行为验收外壳**，评测集同时是 pytest 的夹具数据。

**执行契约不是文本匹配。** 「预期操作状态 / 重试次数 / 平台调用次数 / 幂等回放 / 最终回查 / 副作用数」这些主张，靠读两条事实通道来验证：会话的操作审计 JSONL（重建操作状态与轨迹，例如 `UNKNOWN_OUTCOME → VERIFYING → SUCCEEDED`）与平台的副作用日志（`writes_of("taobao_price_update")` 断言条数）。文本断言只负责「模型说了什么」，不再兼任「系统真的这么跑了吗」。

**故障注入按操作语义定向消费。** 调价路径的快照、回查与写入**都是 POST**（淘宝按信封 `method` 分发、抖店是 JSON body），按 HTTP 方法无法区分读写——不加约束的话，场景里第一次快照预检就会把唯一的写故障步吃掉，重试/超时契约全是假阴性。因此 Adapter 在发起请求前把 `snapshot / verify / apply_price` 写入上下文，故障脚本据此决定这一步该不该消费；操作语义未知时（直接打网关、旧通道）退化为旧语义，不改变既有行为。

四类元数据对应四类问题：`gold`（正常业务结果，两个平台都要覆盖）、`guarded`（RBAC 拒绝 / 低于成本 / 活动锁价 / 平台不支持 / 审批通过 / 审批拒绝）、`resilience`（限流 / 写前超时 / 写后超时 / 持续错误 / 幂等回放 / 畸形响应 / 回查超时）、`recovery`（审批恢复 / 未知结果恢复 / 回查不一致 / 重复批准 / 审计完整性）。领域指标反映调价闭环（`tool_routing`、`price_snapshot_grounding`、`price_verification`、`retry_contract`、`idempotent_key_reuse`、`unknown_outcome_recovery`、`audit_integrity`、`platform_matrix` 等）；标签只参与报告分类，不进入步骤断言，但**平台与期望操作状态属于行为契约，会进入 baseline 指纹**——否则「淘宝改成抖店」「副作用 1 条改成 2 条」这类语义变化会被指纹漏掉，两个平台的状态互相覆盖。

报告同时输出整体结果、按类型汇总、**按平台汇总**和按领域指标汇总。真实模型模式保留 `PASS` / `NOT-EXERCISED` / `FAIL` 三态：模型未触发期望工具不计为执行失败（通过率只算契约被触发的轮次）；如果 guarded 场景重复运行始终未触发，则单独记为 `safe_no_op`，表示"安全地没有发生动作"，而不是把它误报为已验证的权限拦截。独立报告不读写回归基线。

</details>

---

## 📦 功能全景

> 当前默认注册表以**调价闭环**工具为主（快照查询 / 提交改价 / 技能保存）；库存、促销、上下架等属扩展工具通道或 Mock 测试数据，不在默认 Agent 的已验证主链路内（见文末边界声明）。

| 能力 | 说明 |
|---|---|
| 模型后端抽象 | OpenAI 兼容 / Anthropic / Mock 统一 `AgentBackend` 契约，流式增量解析与消息归一化在 adapter 内消化 |
| 多平台调价 | 淘宝 / 抖店两种协议形态收敛到同一份领域命令：查快照 → 提交目标价 → 审批 → 执行 → **平台结果回查**；签名与令牌不出 Adapter |
| 可恢复操作 | 调价有显式状态机与 JSONL 审计；审批挂起 / 执行中断 / 未知结果三种姿态可恢复，复用同一 `operation_id` 与幂等键 |
| 技能体系 | SKILL.md 知识包，渐进式披露控制上下文；内置 10 技能（默认调价闭环 2 条 + 扩展 8 条，扩展技能正文显式声明「需启用扩展工具通道」）+ `save_skill` 元技能热加载创建（经 HITL 确认） |
| 长期记忆 | `MEMORY.md` 索引 + 独立记忆文件，写入经矛盾整合（新增 / 覆盖 / 废弃 / 跳过），跨会话保留经营决策 |
| 上下文压缩 | 超过「模型窗口 − 安全边际」自动压缩为结构化摘要，摘要沉淀为长期记忆 |
| 会话持久化 | Workspace 配置 JSON + Session JSONL 增量落盘，多会话并行，可恢复已落盘消息、工具调用结构和审计记录；不续跑未完成的 turn |
| 渠道管理 | 渠道注册表配置化（base_url / 鉴权 / 平台类型 / 启停），设置页操作即时生效；敏感字段掩码 |
| 审批中心 | 跨会话聚合待审批写操作，对话内权限卡与审批页双入口，REST 决定唤醒挂起中的 Agent |
| 运营看板 | 跨渠道聚合：近 7 天 GMV 趋势、渠道构成、库存水位与经营预警 |
| 双前端 | React 控制台接真实后端；`demo/` 附独立可跑的 mock 前端（无需后端即可演示调价主闭环） |

---

## 🏗️ 架构

链路：前端（React / WebSocket）→ FastAPI → Agent 执行编排（会话与上下文、模型调用、工具循环）→ PreToolUse 权限管线 → Execution Policy（错误分类 / 重试 / 幂等 / 结果校验）→ 工具源 → Commerce Adapter → 离线 Mock 渠道网关。AgentEvent 事件流贯穿前端展示、审计与行为评测。

**第一原则：执行编排不感知「淘宝」。** 平台地址、字段名、签名、错误码语义全部收进 Adapter——接入真实电商平台 = 新增一个 Adapter 实现，上层编排流程不改。

---

## 🗂️ 目录结构

<details>
<summary><b>点击展开</b></summary>

| 模块 | 职责 |
|---|---|
| `transport/` | FastAPI REST + WS 双通道，权限管线与工具源装配 |
| `agent_backend/` | 模型后端抽象与消息归一化 |
| `agent_core/` | 执行核心：回合循环、工具并发执行、权限挂起、上下文压缩与长期记忆 |
| `session/` | Workspace / Session 状态、JSONL 持久化、中断恢复 |
| `permission/` | RBAC 身份门、业务规则、模式门、审计 |
| `sources/` | 默认调价 Tool（快照 / 改价）、扩展工具注册表、技能注册表、渠道注册表、MCP 客户端 |
| `execution/` | 执行策略层（错误分类、超时、重试、幂等键、结果校验）与调价操作状态机 |
| `integrations/` | 调价领域契约与淘宝 / 抖店 Adapter、MCP 客户端池 |
| `mock_commerce/` | 独立第三方电商服务模拟：两套平台协议形态共享商品状态，可独立进程运行 |
| `events/` | 八类 AgentEvent 事件模型 |
| `harness/` | 行为验证 Harness：21 条调价场景数据表、决策 / 执行契约、指纹基线 |
| `frontend/` | React + TS 控制台（对话流、工具活动行、权限卡、看板） |
| `tests/` | 515 个测试，按模块分目录，全部离线运行 |

</details>

---

## 🚀 快速开始

```bash
# 安装（mock 模式无需 API Key）
pip install -e ".[dev]"

# 启动后端（mock 模式）
AGENT_BACKEND=mock python -m uvicorn transport.server:app --port 8000

# 前端开发
cd frontend && npm install && npm run dev   # http://localhost:5173

# 前端构建
cd frontend && npm run build
```

> 真实模型模式：设置 `AGENT_BACKEND=openai`（或 `anthropic`）并配置对应的 `*_API_KEY` / `*_BASE_URL` 环境变量即可。

---

## ✅ 质量与验证

```bash
python -m pytest tests/ -q     # 515 个单元 / 集成 / E2E 测试，全离线
python -m harness              # 21 条调价场景（gold 3 / guarded 7 / resilience 6 / recovery 5）+ 基线验收，退化即非零退出
python -m harness --list                                                # 查看场景类型与领域指标
python -m harness --backend openai --provider <name> --repeat 3   # 真实模型评测（18 条执行契约场景 ×3，三态统计）
```

端到端场景全部走真实执行链：剧本后端替代真实 LLM，Mock API 注入确定性故障，断言到「事件序列 + 工具参数 + 副作用审计」粒度（如：超时重试场景会校验平台写操作日志只有一条）。

**真实模型评测说明：** 当前行为基线以 `harness/cases.py` 中 **21 条调价契约场景** 与 `baseline.json` 为准。真实模型模式只跑其中标注为执行契约的子集（约 18 条 × `--repeat`），结果用 **PASS / NOT-EXERCISED / FAIL** 三态统计：模型未触发期望业务工具时不计为执行失败。不同供应商、不同提示下的触发率与通过率会变化，请以本机 `python -m harness --backend openai --provider <name>` 的报告为准；历史报告仅用于说明评测流程，不代表每次运行数字固定。

---

## ⚠️ 边界声明

1. **业务数据为模拟，平台接入为离线契约实现**：协议层（REST / 认证 / 错误码 / 幂等 / TOP 签名）是真实实现，模拟的是平台返回的数据。淘宝与抖店的 Adapter 与对应 Mock 网关都是离线契约实现，`jd` / `open` 仍是 stub；接入真实电商平台需要平台资质 + 各平台字段映射与签名适配层（真实多平台系统的固有成本），上层编排与评测体系原样复用。**Mock 全绿不等于生产可用。**
2. **本版本的能力面只覆盖商品调价**：客服工单、知识库、经营统计、上下架不属于调价控制面——它们是扩展工具通道里的能力或 Mock 网关的测试数据，默认 Agent 不可见，也不计入这套跨平台抽象的覆盖范围。
3. 摘要与记忆检索当前为规则 / 关键词实现，LLM 摘要与向量检索的升级路径已预留。
4. MCP 通道保留但冻结：已验证平台作为 MCP Client 接入外部工具；调价主链路走 `integrations/commerce` 的调价领域契约与平台 Adapter，两个方向互不污染。
