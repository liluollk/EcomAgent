---
name: anomaly_detection
default: false
description: 经营异常排查（价格/库存/评分）
keywords: [异常, 预警, 排查, 风控]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行异常排查 SOP：
1) 确认渠道；
2) 调用 query_anomalies 拉取异常项；
3) 逐条向用户说明异常并给出处理建议。
