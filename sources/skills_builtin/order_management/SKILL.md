---
name: order_management
default: false
description: 订单状态查询与跟进
keywords: [订单, 发货, 物流]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行订单查询 SOP：
1) 确认渠道与订单号；
2) 调用 query_order_status 查询；
3) 汇报订单当前状态。
