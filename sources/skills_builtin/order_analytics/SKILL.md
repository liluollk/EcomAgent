---
name: order_analytics
default: false
description: 订单/销售分析
keywords: [销售分析, 成交, 销售额, GMV, 订单分析]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行销售分析 SOP：
1) 确认渠道与统计周期；
2) 调用 query_order_stats 获取订单量/GMV/客单价；
3) 汇总关键指标并给出结论。
