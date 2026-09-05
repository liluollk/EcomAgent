---
name: inventory_query
description: 查询商品库存与补货建议
keywords: [库存, 存货, 补货, 盘点]
---

执行库存查询 SOP：
1) 先确认用户指定了渠道与 SKU；
2) 调用 query_inventory 获取当前库存；
3) 向用户汇报库存数量与商品名。