---
name: inventory_query
default: false
description: 查询商品库存与补货建议
keywords: [库存, 存货, 补货, 盘点]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行库存查询 SOP：
1) 先确认用户指定了渠道与 SKU；
2) 调用 query_inventory 获取当前库存；
3) 向用户汇报库存数量与商品名。
