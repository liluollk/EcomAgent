---
name: promotion_management
default: false
description: 促销活动创建与活动检查
keywords: [促销, 优惠, 折扣, 满减, 活动检查]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行促销 SOP：
1) 确认渠道/SKU/折扣/起止时间；
2) 如需检查现有活动先调用 query_promotions；
3) 调用 create_promotion 创建；
4) 汇报活动信息。
