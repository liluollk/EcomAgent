---
name: after_sales
default: false
description: 售后与客诉处理（工单/退款分析）
keywords: [售后, 客诉, 投诉, 退款, 工单]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行售后处理 SOP：
1) 先查订单状态（query_order_status）；
2) 根据客诉内容创建售后工单（service_ticket，注明优先级）；
3) 汇报工单号。
