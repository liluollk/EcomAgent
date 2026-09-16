---
name: knowledge_inquiry
default: false
description: 经营知识库查询
keywords: [知识库, 规范, 话术, 政策]
---

> 注意：本技能依赖默认调价闭环（query_product_snapshot / update_price / save_skill）之外的扩展工具集，需启用扩展工具通道后才可调用对应接口；默认 Agent 不注册这些工具。


执行知识库查询 SOP：
1) 提炼用户主题；
2) 调用 query_knowledge_base 检索；
3) 命中则引用条目回答，未命中则建议补充知识库。
