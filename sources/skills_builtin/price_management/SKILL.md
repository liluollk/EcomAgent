---
name: price_management
description: 商品价格调整管理
keywords: [价格, 调价, 改价, 定价, 降价]
---

执行调价 SOP：
1) 确认目标价与成本价；
2) 若新价格低于成本价则向用户说明无法执行（成本保护规则）；
3) 调用 update_price 调整价格；
4) 汇报新旧价格。