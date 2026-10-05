# 商品平台 REST Adapter 设计

状态：待评审

## 目标

在现有商品运营 Agent 与 Runtime 边界上，增加一个基于 HTTP REST 的商品平台 Adapter 实现，使商品写操作能够经统一 `CommerceAdapter` 协议执行和回查。通过本地 Mock REST 服务验证接口、幂等和故障恢复行为，为简历中的“商品平台 Adapter 接入 REST API”提供可复现的代码与测试依据。

本设计不声称完成淘宝或抖音官方 API 的授权、签名或真实店铺联调。

## 边界与职责

```text
商品运营 Agent 的 Local Tool（例如 update_price）
    ↓ ToolBinding
Runtime（PreToolUse、权限/规则/确认、Operation、恢复与审计）
    ↓ CommerceAdapter
RestCommerceAdapter（商品平台 REST 协议映射）
    ↓ HTTP
受控的商品平台 API
```

- Local Tool 定义商品运营动作及其模型可见参数。
- Runtime 保持唯一的写操作授权、确认、状态迁移与重试/回查决策者。
- `CommerceAdapter` 负责把已授权的商品命令转换为平台请求，并将回执归一化。
- MCP 仍由 `Source` 管理和装载；本设计不把商品平台强行建模成通用 MCP 或动态 REST Source。
- Skill 描述任务方法，不承担平台 API 配置或商品事实存储。

## 范围

首期仅覆盖已有商品写能力 `update_price`，实现专用、固定语义的 REST Adapter 与 Mock REST 服务契约测试。保留现有 `MockCommerceAdapter` 作为快速确定性测试实现。

明确不做：

- 任意 URL、任意 HTTP 方法或任意 OpenAPI 文档生成工具；
- 可配置的通用 REST Source/低代码 API 平台；
- 淘宝、抖音官方 API 的真实授权与上线联调；
- 将 REST Adapter 的传输、平台协议或业务规则放入 Runtime 核心。

## Adapter 合同

1. `execute` 接收完整的 `UpdatePriceCommand`，包含 Workspace、店铺、Listing、目标价格、币种、期望版本和稳定 `operation_id`；
2. `reconcile` 接收可独立定位平台资源的持久化操作上下文，不允许 Adapter 依赖进程内临时映射或直接读取 Runtime 数据库；
3. 网络超时、连接中断或无法判断是否已生效时返回 `UNKNOWN`；Runtime 负责安排回查，禁止 Adapter 内部盲目重试写操作；
4. 平台明确拒绝、版本冲突等结果映射为确定失败；成功回执只依据平台响应或权威回查；
5. `count_logical_writes` 是测试观测能力，不属于生产 `CommerceAdapter` 协议。重复写次数由 Mock REST 服务的权威操作记录断言。

操作上下文应从 Runtime 已持久化的 `Operation` 与对应 `ToolCall` 构造，并确保按待回查的具体 `operation_id` 定位，不能只依赖“某 Run 最新的 ToolCall”。

## REST 契约与安全

Mock REST 服务提供固定商品接口：按外部店铺和 Listing 修改价格、使用稳定幂等键、携带期望版本，并支持按操作标识查询执行结果或按资源读取权威状态。具体路径和字段在实施计划中固定，不允许模型提供 URL 或认证信息。

生产 Adapter 的端点只来自受信任部署配置/允许列表；远程连接要求 HTTPS。认证凭证通过现有 Secret 引用解析，明文不得进入 Tool 参数、事件、审计、Trace 或异常日志。连接超时、读取超时和响应体大小均设上限，不跟随跨主机重定向。

如果目标平台不提供幂等能力，也没有可靠的操作状态查询或资源回查手段，该平台 Adapter 必须将能力标记为不支持可靠写入，不得把本地 Mock 的保证外推为平台保证。

## 验收标准

- REST Adapter 与 Mock Adapter 实现同一生产协议；测试计数接口仅存在于测试实现。
- 成功写入可回读权威价格与版本；幂等重放不会产生第二次逻辑写。
- 版本冲突确定失败；写后响应丢失返回 `UNKNOWN`，由 Runtime 回查后恢复，不盲重写。
- 无法回查时保持 `UNKNOWN` 并按既有 Runtime 策略终止/告警，不臆断成功或失败。
- 测试覆盖请求映射、认证头脱敏、超时、5xx/4xx 分类、重定向/非允许端点拒绝、幂等、响应丢失回查及跨 Workspace/店铺边界。
- 完成后更新架构文档、实施计划、Eval 夹具和 README；README 明确报告 REST 验证使用本地 Mock 服务，真实平台联调尚未完成。

## 简历口径

实现并验证后可写“以商品平台 Adapter 对接 REST API，并通过幂等键、版本校验与回查处理不确定写入”。不得写“已接通淘宝/抖音真实平台”或将该 Adapter 描述为任意 REST API 工具生成平台。
