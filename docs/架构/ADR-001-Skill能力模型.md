# ADR-001：Skill 能力模型

- **状态：** 已接受，尚未实施
- **决策日期：** 2026-10-12
- **适用范围：** Runtime Skill Catalog、Skill 激活、上下文装载、版本固定与商品 Agent 的 Skill 使用方式
- **不包含：** 本 ADR 不修改源码、测试、评测结果或简历正文，也不决定 Skill 的最终命名

## 1. 决策摘要

Skill 支持是 Runtime 的必要能力，但某个 Skill 不是每个 Agent 或每次任务的必需依赖。

Skill 被定义为可发现、可按需激活的指令资源：Runtime 向模型展示当前可见 Skill 的名称和描述，模型根据任务判断是否加载完整指令；用户也可以显式激活 Skill。Skill 可以指导模型如何使用 Tool，但不授予 Tool 权限，也不承担确定性安全规则。

项目保留服务端场景需要的版本固定、激活事件和恢复机制，但撤销“Agent 必须依赖某个 Skill”“Skill 必须依赖某组 Tool/Source”和“商品任务必须先加载固定 Skill”的语义。

## 2. 参考实现与适配原则

本决策参考 Pi Coding Agent 当前源码提交 `4270522473e677ddba7b093caa0064a3df51d459` 及 Agent Skills 规范。

Pi 的核心模型是：

1. 从用户、项目、Package 或显式路径发现 `SKILL.md`；
2. 解析 `name`、`description`、路径和调用控制信息；
3. System Prompt 只展示 Skill Catalog，不预先注入完整正文；
4. 模型在任务匹配时读取完整 `SKILL.md`；
5. 用户可通过显式命令直接激活 Skill；
6. Skill 是指令资源，不是 Tool，也不是权限授予单元。

本项目不照搬 Pi 的本地文件实现。由于业务 Runtime 不向模型开放服务器文件系统，使用 Runtime 自有的 `load_skill` 控制能力返回 Skill 指令；由于项目需要故障恢复和评测复现，继续固定 Run 可见的 SkillVersion，并记录 Skill 激活事实。

## 3. 当前代码事实

当前第一版实现包含以下行为：

1. `AgentDefinition.required_skill_keys` 把 Skill 建模为 Agent 的必需依赖；
2. `SkillDefinition.required_tool_keys` 和 `required_source_keys` 把 Skill 与 Tool/Source 建立配置依赖；
3. 商品 Agent 把 `product_maintenance` 放入 `required_skill_keys`；
4. 商品 Prompt 强制每个商品任务先调用 `load_skill(product_maintenance)`；
5. `load_skill` 被放入商品 Agent 的业务 Tool 集合；
6. Context 只常驻 Skill Catalog，完整指令在激活后进入受保护上下文；
7. Run 固定 SkillVersion，`SKILL_ACTIVATED` 事件用于恢复激活状态；
8. 商品装配目前从整个 Skill Registry 构造 Catalog，而不是从当前 Run 的可见 SkillVersion 构造。

其中第 6、7 项方向成立；第 1、2、3、4、5、8 项属于待调整实现。

## 4. 问题分析

### 4.1 必需依赖与按需激活互相矛盾

`required_skill_keys` 表示 Agent 缺少该 Skill 就不能完成组装；Prompt 又要求模型每次显式加载同一个 Skill。这种设计名义上是 Progressive Disclosure，实际上只是给所有任务增加一次固定 ToolCall。

### 4.2 Skill 与 Tool/Source 职责混合

Tool 是可执行能力，Source 是 Tool 或知识能力的来源，Skill 是任务方法和领域指导。Skill 可以在正文中建议模型使用某个 Tool，但 Tool 的可见性、授权和执行资格必须由 Agent、Workspace、RBAC 与 Runtime 决定。

### 4.3 Runtime 控制能力混入业务 Tool

`load_skill` 不执行业务动作，也不应与 `search_products`、`compare_listing_prices`、`update_price` 处于同一业务能力集合。它属于 Runtime Control Tool，应由 Runtime 根据当前 Run 是否存在可见 Skill 自动暴露。

### 4.4 Catalog 与实际可加载范围可能不一致

当前 Catalog 来自整个 Skill Registry，而 `load_skill` 只允许激活 Run 已固定的 SkillVersion。Skill 数量增加后，模型可能看到一个无法加载的 Skill。Catalog 必须由当前 Run 的可见版本快照构造。

### 4.5 Skill 内容混入实现细节

当前 `product_maintenance` Skill 同时描述业务方法、ExecutionMode、参数来源校验、内部 ID 限制和恢复行为。Skill 应保留稳定的任务方法；权限、执行模式、幂等、UNKNOWN 和 Reconciliation 等确定性规则由 Runtime 与业务服务实现。

## 5. 目标能力模型

### 5.1 Agent

Agent 定义业务角色、Agent Prompt、可用业务 Tool、Source 范围以及可见的 Skill Catalog。Agent 可以没有匹配当前任务的 Skill，模型仍可直接使用 Tool。

目标语义示意：

```python
class AgentDefinition:
    name: str
    system_prompt_version: str | None
    required_backend_capabilities: frozenset[str]
    required_tool_keys: frozenset[str]
    required_source_keys: frozenset[str]
    skill_catalog_keys: frozenset[str]
```

`skill_catalog_keys` 是本 ADR 确定的目标字段名，表示可见范围，不表示必须激活。本次不调整 Tool、Source 的装配字段语义。

### 5.2 Skill

Skill 是包含元数据和完整指令的版本化资源：

```python
class SkillDefinition:
    name: str
    description: str
    instructions: str
    disable_model_invocation: bool = False
```

- `description` 同时说明 Skill 做什么、何时使用，供模型路由；
- `instructions` 在激活时进入上下文；
- `disable_model_invocation=True` 时不向模型展示，只允许用户或系统显式激活；
- Skill 不声明 Tool/Source 必需依赖，不授予权限。

### 5.3 Tool 与 Source

- Business Tool 执行真实业务动作；
- Source 表示本地、REST API 或 MCP 等能力来源；
- Agent/Workspace 决定 Tool 和 Source 的可见范围；
- PreToolUse 决定一次 ToolCall 能否执行；
- Skill 只提供使用方法，不改变上述边界。

### 5.4 Runtime Control Tool

`load_skill` 由 Runtime 管理：

1. 当前 Run 的 Skill Catalog 非空时才向模型暴露；
2. 输入只能选择当前 Run 可见的 `skill_key`；
3. 重复激活同一版本必须幂等；
4. 激活失败返回结构化错误，不扩大 Skill 或 Tool 范围；
5. 它不进入商品 Agent 的 Business Tool 列表，也不经过商品写操作确认。

### 5.5 Run 固定与激活恢复

需要区分“可见”和“已使用”：

```text
RunSkillVersion
  = 本次 Run 可以看到和加载的 SkillVersion

SKILL_ACTIVATED
  = 本次 Run 实际激活过的 SkillVersion
```

Run 创建时固定可见版本；Context Catalog 只使用这些固定版本。激活后记录事件，恢复时根据固定版本与激活事件重建受保护的 Skill 指令。Skill 更新只影响后续 Run，不改变正在执行或恢复中的 Run。

## 6. 目标执行链路

```text
创建 Run
  → 固定当前可见 SkillVersion
  → Context 只展示 name + description
  → 模型根据任务判断是否需要 Skill
      → 不需要：直接选择 Business Tool
      → 需要：调用 Runtime load_skill
          → 校验 Skill 属于当前 Run
          → 记录 SKILL_ACTIVATED
          → 注入完整 instructions
          → 继续选择 Business Tool
  → PreToolUse 执行权限、资源范围、参数、确认和业务规则校验
  → Tool 执行
  → PostToolUse / Reconciliation 处理结果
```

System Prompt 只包含通用 Skill 协议，不得点名固定业务 Skill：

```text
以下 Skills 为特定任务提供专门指令。
当任务与某个 Skill 的 description 匹配时，调用 load_skill 加载完整指令。
```

## 7. 商品运营 Skill 的定位

商品查询可以直接使用查询 Tool，不应为了形式统一而加载 Skill。

只有需要专门任务方法的复杂场景才使用 Skill，例如：

- 先查询并比较多个店铺，再根据用户条件选择目标并改价；
- 多轮补全商品、店铺和目标价格后执行受控改价；
- 需要遵循一套稳定运营方法，但步骤仍允许模型根据上下文调整。

商品 Skill 应描述：

- 适用任务；
- 信息收集顺序；
- 需要澄清的条件；
- 如何判断任务完成；
- 业务方法中的常见异常情况。

商品 Skill 不应描述：

- RBAC、店铺授权和确认状态的内部实现；
- 价格阈值等可变业务事实；
- 数据库内部 ID 和模型评测补丁；
- 幂等、租约、UNKNOWN 和 Reconciliation 的实现细节。

如果某个步骤必须由系统确定性保证，应放入 Workflow、PreToolUse、Tool 或业务服务，而不是依赖 Skill 指令。

## 8. 面试口径与事实边界

简历正文暂不修改。当前代码确实实现了 Skill Catalog、按需加载接口、版本固定和激活恢复，评测优化也确实调整过 Skill；但不能把本 ADR 的目标模型描述成已经落地。

推荐回答：

> 第一版实现了 Skill Catalog、`load_skill`、Run 级 Skill 版本快照和激活恢复。当时为了让唯一的商品 Skill 稳定生效，把它配置成 Agent 必需依赖，并在 Prompt 中强制加载。后来对照 Pi 和 Agent Skills 的实现复盘后，我发现这种做法会让 Progressive Disclosure 流于形式，也混淆了 Skill 与能力装配。最终方案是让 Agent 只限定可见的 Skill Catalog，由模型按任务选择；`load_skill` 归 Runtime 管理，Skill 不授予 Tool 权限。该方案已经完成设计定稿，代码重构尚未实施。

面试时禁止使用以下表述：

- “当前代码已经完全采用 Pi 的 Skill 模型”；
- “每个 Agent 都必须绑定至少一个 Skill”；
- “所有商品任务都会按需选择 Skill”，因为当前 Prompt 实际仍强制加载；
- “Skill 决定 Agent 拥有哪些 Tool 权限”；
- “目标设计已经通过现有评测”，因为重构后尚未重新运行真实模型评测。

如果面试官询问为何暂未重构，应回答：先冻结概念和事实边界，避免继续以补丁方式改动；实施时会先调整模型和确定性测试，再重新运行真实模型评测，不能直接继承旧结果。

## 9. 实施前验收标准

未来进入实现阶段时，至少满足：

1. 商品 Prompt 不再点名或强制加载固定 Skill；
2. Skill 不再声明 Tool/Source 必需依赖；
3. Agent 配置表达 Skill 可见范围，而不是必需激活关系；
4. `load_skill` 不再属于商品 Business Tool 集合；
5. Catalog 只展示当前 Run 固定且允许加载的 SkillVersion；
6. 无可见 Skill 时不向模型暴露 `load_skill`；
7. 简单商品查询可以在不激活 Skill 的情况下完成；
8. 复杂改价任务可以选择并激活对应 Skill；
9. 重复激活幂等，未知或越界 Skill 确定性失败；
10. Run 恢复后可以重建已激活 Skill 指令；
11. Skill 不能扩大 Tool 权限或绕过 PreToolUse；
12. 真实模型评测重新执行后，才更新评测报告和面试数字解释。

## 10. 非目标

本决策不引入通用 Workflow Engine、LangGraph、Multi-Agent、Skill Router、自动关键词匹配或新的外部基础设施。Skill 是否匹配任务仍由模型根据 Catalog 描述判断；确定性业务流程在出现真实需求后单独设计。

## 11. 参考资料

- Pi Coding Agent：`packages/coding-agent/src/core/skills.ts`
- Pi Coding Agent：`packages/coding-agent/src/core/system-prompt.ts`
- Pi Coding Agent：`packages/coding-agent/src/core/agent-session.ts`
- Pi Coding Agent：`packages/coding-agent/src/core/resource-loader.ts`
- Agent Skills Specification：<https://agentskills.io/specification>
- Agent Skills Client Implementation：<https://agentskills.io/client-implementation/adding-skills-support>
