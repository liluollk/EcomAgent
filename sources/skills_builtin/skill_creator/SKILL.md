---
name: skill_creator
description: 创建新的运营技能（SKILL.md 知识包）
keywords: [创建技能, 做成技能, 生成技能, 写个技能, 沉淀]
---

执行技能创建 SOP：
1) 与用户确认技能目标：要沉淀哪类运营流程 / 平台规则；
2) 起草技能内容：name（小写字母开头的下划线标识）、description（一句话描述，
   用于技能菜单展示与意图识别）、keywords（触发关键词）、body（分步 SOP 正文，
   包含前置确认、真实工具名引用如 query_inventory / update_price、结果汇报）；
3) 调用 save_skill 保存（写操作，需人工确认后落盘为 SKILL.md）；
4) 汇报新技能名与技能菜单变化。