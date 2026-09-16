# 多平台商品调价控制层实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 将 EcomAgent 从 11 个混合业务 Tool 通过 CommerceProvider 调用 Mock API，收敛为淘宝/抖店双平台商品调价控制层，打通统一领域命令、权限审批、平台 Adapter、可靠执行、结果回查和调价闭环评测。

**Architecture:** 保留现有 Session、AgentBackend、PreToolUse、AgentEvent 和 Execution Policy 作为通用 Agent 基础设施；新增商品调价领域契约与可恢复操作状态，把平台差异收敛到 TaobaoAdapter/DouyinAdapter。默认 Tool Registry 只暴露调价闭环需要的语义 Tool，客服、知识库、经营分析等边界能力不再挂在 CommerceProvider 或主评测链路上。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、pytest、React/TypeScript、JSONL/JSON 持久化、现有 Mock Commerce API

---

## 文件和职责总览

新增：

- integrations/commerce/price_models.py：商品调价领域命令、快照、约束、回查结果和统一平台错误类型。
- integrations/commerce/capabilities.py：平台能力画像，声明淘宝和抖店是否支持查询、改价、回查以及价格精度等约束。
- execution/price_change.py：商品调价操作协调器，负责状态流转、规则数据装配、平台写入和结果回查。
- session/operation_store.py：调价操作记录的 JSONL 持久化，支持审批挂起、执行中断和未知结果恢复。
- tests/integrations/test_price_contracts.py、tests/integrations/test_price_adapters.py：领域契约和双平台 Adapter 测试。
- tests/execution/test_price_change.py、tests/session/test_operation_store.py：状态机和持久化测试。
- tests/integrations/fixtures/taobao_price_contract.json、douyin_price_contract.json：平台差异 Fixture。

修改：

- integrations/commerce/interface.py、models.py、provider.py：收敛核心平台接口，并移除客服、知识库、分析能力对 CommerceProvider 的依赖。
- integrations/commerce/adapter.py、client.py、sources/channel_registry.py：实现淘宝/抖店调价请求、响应、鉴权和错误映射。
- sources/builtin_tools.py、permission/tool_policy.py、permission/rule_engine.py：默认只装配调价闭环 Tool。
- session/session.py、session/storage.py、agent_core/base_agent.py、events/agent_event.py：关联 operation_id，支持审批和执行状态审计。
- mock_commerce/routes.py、store.py、fault_injection.py、domain.py：提供两个平台形态不同的 Mock 协议和调价故障。
- harness/cases.py、assertions.py、metrics.py、report.py、runner.py、faults.py：把 21 个场景改成调价闭环矩阵。
- sources/skills_builtin/price_management/SKILL.md、README.md 和必要的前端展示：同步新的主线和边界。

## Task 1：建立调价领域契约和平台能力画像

**Files:** 新建 integrations/commerce/price_models.py、integrations/commerce/capabilities.py、tests/integrations/test_price_contracts.py；修改 integrations/commerce/interface.py、models.py。

- [ ] 写失败测试，固定 ProductRef、ProductSnapshot、PriceConstraints、PriceChangeCommand、PriceVerification 五个领域对象。
- [ ] 断言目标价使用 Decimal；PriceChangeCommand 不包含平台私有字段；平台能力缺失返回 CAPABILITY_UNSUPPORTED。
- [ ] 运行 pytest tests/integrations/test_price_contracts.py -q，确认当前实现失败。
- [ ] 实现统一领域对象。PriceChangeCommand 至少包含 operation_id、product_ref、target_price、requester、reason；ProductRef 至少包含 platform、shop_id、product_id、sku_id。
- [ ] 实现 PlatformCapabilities，至少包含 query_snapshot、update_price、verify_price、price_scale、requires_signature。
- [ ] 为 taobao 和 douyin 注册不同能力画像，mock 只作为离线测试平台。
- [ ] 运行上述测试并提交：git add integrations/commerce tests/integrations/test_price_contracts.py；git commit -m "refactor: add cross-platform price contracts"。

## Task 2：默认 Tool 收敛到调价闭环

**Files:** 修改 sources/builtin_tools.py、permission/tool_policy.py、sources/skills_builtin/price_management/SKILL.md；修改相关默认 Skill 和 tests/sources/test_builtin_tools.py、tests/sources/test_batch4_tools.py。

- [ ] 先写测试，默认 get_definitions 和 get_handlers 只暴露 query_product_snapshot、update_price、save_skill。
- [ ] 将 verify_price_change 和 query_price_constraints 设计为协调器内部能力，不作为模型自由选择的 Tool。
- [ ] 断言 service_ticket、query_knowledge_base、query_anomalies、query_after_sales_stats 不在默认工具列表；扩展工具保留独立注册入口但不参与默认 Agent。
- [ ] 运行相关测试，确认旧注册表失败。
- [ ] 将 query_inventory 改造成商品快照入口，返回当前价、库存、商品状态和活动信息；旧工具如需兼容，放到扩展映射。
- [ ] 更新 price_management Skill，流程固定为查询快照、提交目标价、等待审批、执行和回查；其他 Skill 标记为非默认扩展，不能引导默认 Agent 调用未注册工具。
- [ ] 运行 pytest tests/sources/test_builtin_tools.py tests/sources/test_batch4_tools.py tests/permission -q。
- [ ] 提交：git add sources permission tests/sources tests/permission；git commit -m "refactor: scope default tools to price workflow"。

## Task 3：实现淘宝和抖店 Adapter 的调价契约

**Files:** 修改 integrations/commerce/adapter.py、client.py、sources/channel_registry.py、tests/integrations/test_platform_adapter.py；新建 tests/integrations/test_price_adapters.py 和两个平台 Fixture。

- [ ] 写平台差异测试：淘宝使用 TOP 风格参数信封、商品/SKU 字段和错误响应；抖店使用 JSON 请求体、访问令牌/签名字段、商品/SKU 字段和另一套错误响应。
- [ ] 断言同一个 PriceChangeCommand 经过两个 Adapter 后，请求形态不同，但都能解析为统一领域结果；平台错误统一映射为客户端错误、业务错误、瞬态错误或未知结果。
- [ ] 运行现有 Adapter 测试和新增测试，确认真实平台目前仍是 stub。
- [ ] 为两个 Adapter 实现商品快照查询、改价、改价回查、响应解析和错误解析。
- [ ] 鉴权参数只在 Adapter/Client 内生成；模型和领域命令不可传入签名、access token 或平台私有字段。
- [ ] 使用 Fixture 和 Mock 网关验证请求构造、响应解析、错误码映射和价格精度。
- [ ] 运行 pytest tests/integrations/test_platform_adapter.py tests/integrations/test_price_adapters.py tests/integrations/test_rest_channel.py -q。
- [ ] 提交：git add integrations tests/integrations；git commit -m "feat: add taobao and douyin price adapters"。

## Task 4：增加可恢复的调价操作状态机

**Files:** 新建 execution/price_change.py、session/operation_store.py、tests/execution/test_price_change.py、tests/session/test_operation_store.py；修改 session/session.py、session/storage.py、agent_core/base_agent.py、events/agent_event.py。

- [ ] 写失败测试，覆盖 CREATED → PRECHECKED → WAITING_APPROVAL → EXECUTING → VERIFYING → SUCCEEDED。
- [ ] 覆盖 BLOCKED、REJECTED、RETRYING、UNKNOWN_OUTCOME → VERIFYING → SUCCEEDED 等异常路径，并拒绝非法状态转移。
- [ ] 实现按 session 隔离的操作 JSONL，记录 operation_created、state_changed、approval_decided、platform_attempt、verification_observed。
- [ ] 实现 PriceChangeCoordinator，顺序固定为能力检查、查询快照、查询内部价格约束、规则校验、审批、平台写入、结果回查。
- [ ] 写入超时不得直接结束为失败；必须进入 UNKNOWN_OUTCOME 并回查，恢复时复用同一个 operation_id 和幂等键。
- [ ] 在 permission_request 事件和 session.permission_requests 中增加 operation_id、平台、商品引用、目标价和规则摘要；审批批准后恢复同一操作。
- [ ] 运行 pytest tests/execution/test_price_change.py tests/session/test_operation_store.py tests/agent_core/test_base_agent_permission.py -q。
- [ ] 提交：git add execution session agent_core events tests/execution tests/session tests/agent_core；git commit -m "feat: add recoverable price change operation"。

## Task 5：扩展 Mock Commerce API 覆盖双平台故障

**Files:** 修改 mock_commerce/routes.py、store.py、fault_injection.py、domain.py、tests/mock_commerce/test_fault_injection.py；新建 tests/mock_commerce/test_platform_price_routes.py。

- [ ] 写测试，确认淘宝和抖店使用不同请求字段和响应信封，但驱动同一份商品状态、价格回查结果和副作用日志。
- [ ] 实现淘宝风格和抖店风格 Mock 路由，底层共享商品状态，返回平台各自错误码和成功结构。
- [ ] Mock 不接受模型传入的成本价作为权威数据；成本数据必须来自 CostProvider。
- [ ] 增加一次限流后成功、写入前超时、写入后超时、持续业务错误、平台能力拒绝、畸形成功响应、回查不一致、回查超时等确定性脚本。
- [ ] 断言写入后超时保留一次副作用日志，重试命中同一幂等键不得新增第二条。
- [ ] 运行 pytest tests/mock_commerce tests/integrations/test_price_adapters.py -q。
- [ ] 提交：git add mock_commerce tests/mock_commerce；git commit -m "test: model cross-platform price failures"。

## Task 6：将 Evaluation Harness 重构为调价闭环矩阵

**Files:** 修改 harness/cases.py、assertions.py、metrics.py、report.py、runner.py、faults.py 和 tests/harness 下对应测试；新建 tests/harness/test_price_matrix.py。

- [ ] 将 21 条场景全部改为商品调价场景，按 gold、guarded、resilience、recovery 四类组织。
- [ ] gold 覆盖查询、改价、回查和两个平台成功；guarded 覆盖 RBAC、低于成本、活动锁价、平台不支持、审批通过和审批拒绝。
- [ ] resilience 覆盖限流、写前超时、写后超时、持续错误、幂等回放、畸形响应和回查超时；recovery 覆盖审批恢复、未知结果恢复、回查不一致、重复批准和审计完整性。
- [ ] 每条场景声明平台、输入、工具序列、预期操作状态、重试次数、副作用数和指标标签。
- [ ] 决策契约只断言模型是否选择正确 Tool、是否传递 SKU/目标价；执行契约断言权限、状态流转、平台调用次数、幂等回放、最终回查和副作用数。
- [ ] 保留 PASS、NOT-EXERCISED、FAIL 三态；预检读取不判错，未触发目标 Tool 不计为执行失败。
- [ ] Baseline 指纹必须包含平台和操作状态，避免两个平台相互覆盖。
- [ ] 运行 pytest tests/harness -q、python -m harness --list、python -m harness。
- [ ] 预期 21 条调价场景全部通过，真实模型模式只运行执行契约子集。
- [ ] 提交：git add harness tests/harness；git commit -m "refactor: evaluate price workflow by platform matrix"。

## Task 7：清理边界、文档和默认展示

**Files:** 修改 README.md、sources/channel_registry.py、demo/README.md、前端相关 Skill 页面和对应测试。

- [ ] 删除“11 个平台操作全部通过 CommerceProvider”“经营分析和知识库属于平台能力”等表述。
- [ ] README 明确主闭环为多平台商品调价，并说明淘宝/抖店 Adapter 是离线契约实现还是具备真实凭证的生产接入。
- [ ] 前端和 Demo 只展示调价相关 Tool、平台能力、审批信息和结果回查；扩展 Tool 不出现在主流程示例。
- [ ] 文档统一展示：Agent Tool → 调价领域命令 → 策略/审批 → 操作状态机 → Taobao/Douyin Adapter → 平台 API → 回查。
- [ ] 明确成本数据属于内部 CostProvider；客服工单、知识库、统计分析不是本版本 CommerceProvider 能力；Mock 验证不等于真实生产接入。
- [ ] 运行 pytest tests/ -q 和 python -m harness。
- [ ] 预期全部核心测试及 21 条调价场景通过，文档、默认 Tool、平台和评测数量与代码一致。
- [ ] 提交：git add README.md demo frontend sources/channel_registry.py tests；git commit -m "docs: align project boundary with price control plane"。

## 最终验证清单

- [ ] 默认模型只能看到调价闭环 Tool 和元技能，不会看到客服工单、知识库、经营分析 Tool。
- [ ] 同一个 PriceChangeCommand 可分别经过淘宝和抖店 Adapter，平台私有字段不泄漏到领域层。
- [ ] 两个平台的不同错误码都能映射到统一错误分类。
- [ ] ASK 审批批准后继续同一个 operation_id，拒绝不会产生平台副作用。
- [ ] 写入后客户端超时会进入 UNKNOWN_OUTCOME 并回查，不会盲目产生第二次写入。
- [ ] 调价成功必须经过平台结果回查，不能只依据写接口成功返回。
- [ ] 21 条 Harness 场景按平台和契约分层统计，NOT-EXERCISED 不计为执行失败。
- [ ] README、技能、前端展示和简历口径都不把 Mock 验证描述成真实生产平台接入。
