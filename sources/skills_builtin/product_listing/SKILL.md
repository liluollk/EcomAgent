---
name: product_listing
default: false
description: 商品上架/下架管理
keywords: [上架, 下架, 新品发布, 下柜]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行上下架 SOP：
1) 确认渠道/SKU/动作（on=上架 off=下架）；
2) 可选先查库存；
3) 调用 product_shelf 执行；
4) 汇报结果。
