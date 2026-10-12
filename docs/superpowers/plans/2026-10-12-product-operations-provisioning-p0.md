# 商品运营能力发布与应用装配分离 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 把 Agent、Skill、Tool 和 ModelConnection 的版本发布从 build_product_application 中移出，使普通应用装配成为只读操作，ScriptedBackend 配置只存在于显式 E2E、Eval 和测试入口。

**Architecture:** ProductOperationsProvisioner 负责幂等发布版本化配置并返回精确 Version ID 快照；ProductOperationsComposition 只接收快照、构造 Registry、Runtime、Handler 和 Adapter。Provisioning 与 Composition 可以处于同一模块化单体，但具有不同调用入口和数据库权限语义。

**Tech Stack:** Python 3.12、Pydantic、SQLAlchemy Async、PostgreSQL、pytest、pytest-asyncio、Ruff、mypy

---

## 范围与约束

- 不修改 Runtime 主循环；
- 不修改 Skill 目标模型；
- 不新增真实平台或真实模型连接；
- 保持现有 Agent/Skill/Tool stable_key 和 version；
- 不把 E2E scripted 地址迁入普通 WorkerSettings 默认值；
- 本计划与 runtime-tool-binding-p0 计划独立提交、独立验收。

## 文件结构

**新建：**

- composition/provisioning/__init__.py：导出 Provisioning 公共入口。
- composition/provisioning/product_operations.py：发布值对象、幂等发布服务和已发布能力快照。
- tests/composition/test_product_operations_provisioning.py：发布幂等、只读装配与 scripted 隔离测试。

**修改：**

- composition/product_operations.py：删除 _bootstrap_capabilities，要求传入已发布快照。
- apps/worker/e2e.py：显式发布 scripted 测试配置后再装配。
- eval/fixtures.py：每个隔离 Eval 环境显式发布固定配置。
- tests/agents/product_operations/conftest.py：测试夹具先发布再装配。
- tests/architecture/test_dependency_boundaries.py：禁止普通组合根写配置表。

### Task 1: 用测试锁定 Provisioning 与 Composition 的边界

**Files:**

- Create: tests/composition/test_product_operations_provisioning.py

- [ ] **Step 1: 编写发布幂等测试**

同一个 workspace、同一版本规范连续发布两次，返回相同 Version ID，配置表行数不增长：

```python
first = await provisioner.provision(
    workspace_id=workspace_id,
    model=scripted_model_spec,
)
second = await provisioner.provision(
    workspace_id=workspace_id,
    model=scripted_model_spec,
)

assert second == first
assert await configuration_counts(session, workspace_id) == expected_counts
```

- [ ] **Step 2: 编写普通装配只读测试**

```python
before = await configuration_snapshot(session, workspace_id)
application = await build_product_application(
    engine,
    capabilities=published_capabilities,
    backend=backend,
    workspace_id=workspace_id,
    actor_id=actor_id,
    membership_id=membership_id,
    price_config=price_config,
)
after = await configuration_snapshot(session, workspace_id)

assert after == before
await application.aclose()
```

- [ ] **Step 3: 编写未发布快照拒绝测试**

给 build_product_application 传入不存在的 Version ID，断言抛出 AssemblyError，且数据库没有自动创建任何配置行。

- [ ] **Step 4: 运行测试并确认失败**

Run:

```powershell
pytest tests/composition/test_product_operations_provisioning.py -q
```

Expected: FAIL，因为 _bootstrap_capabilities 仍在普通组合根中执行写入，Provisioner 尚不存在。

- [ ] **Step 5: 提交失败测试**

```powershell
git add tests/composition/test_product_operations_provisioning.py
git commit -m "test: define product provisioning boundary"
```

### Task 2: 建立 ProductOperationsProvisioner

**Files:**

- Create: composition/provisioning/__init__.py
- Create: composition/provisioning/product_operations.py
- Modify: composition/product_operations.py
- Modify: tests/composition/test_product_operations_provisioning.py

- [ ] **Step 1: 定义发布输入与输出值对象**

```python
@dataclass(frozen=True)
class ModelConnectionProvisioningSpec:
    stable_key: str
    backend_type: ModelBackendType
    base_url: str
    model: str
    non_secret_options: Mapping[str, Any]


@dataclass(frozen=True)
class ProductOperationsCapabilities:
    agent_version_id: UUID
    skill_version_ids: Mapping[str, UUID]
    tool_version_ids: Mapping[str, UUID]
    model_config_version_id: UUID
```

- [ ] **Step 2: 移动现有发布逻辑**

把 composition/product_operations.py 中 _bootstrap_capabilities 的完整数据库写入移动到 ProductOperationsProvisioner。按以下四个私有方法拆分，但不改变查询条件、版本号、状态或幂等语义：

```python
class ProductOperationsProvisioner:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def provision(
        self,
        *,
        workspace_id: UUID,
        model: ModelConnectionProvisioningSpec,
    ) -> ProductOperationsCapabilities:
        session = self._session_factory()
        try:
            async with session.begin():
                agent_version_id = await self._provision_agent(session)
                skill_version_ids = await self._provision_skills(session)
                tool_version_ids = await self._provision_tools(session)
                model_config_version_id = await self._provision_model(
                    session,
                    workspace_id=workspace_id,
                    spec=model,
                )
            return ProductOperationsCapabilities(
                agent_version_id=agent_version_id,
                skill_version_ids=skill_version_ids,
                tool_version_ids=tool_version_ids,
                model_config_version_id=model_config_version_id,
            )
        finally:
            await session.close()
```

四个私有方法直接接收 session；不得在方法内部再次开启事务或创建 Session。

- [ ] **Step 3: 删除 Provisioner 中的 scripted 默认值**

base_url、model、backend_type 和 non_secret_options 只能来自 ModelConnectionProvisioningSpec。Provisioner 不提供 product-scripted 或 scripted.model.invalid 默认值。

- [ ] **Step 4: 运行发布测试**

Run:

```powershell
pytest tests/composition/test_product_operations_provisioning.py -k provision -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add composition/provisioning composition/product_operations.py tests/composition/test_product_operations_provisioning.py
git commit -m "refactor: extract product capability provisioner"
```

### Task 3: 让普通组合根只消费已发布快照

**Files:**

- Modify: composition/product_operations.py
- Modify: apps/worker/e2e.py
- Modify: eval/fixtures.py
- Modify: tests/agents/product_operations/conftest.py
- Modify: tests/composition/test_product_operations_provisioning.py

- [ ] **Step 1: 修改 build_product_application 签名**

增加必填参数：

```python
capabilities: ProductOperationsCapabilities
```

删除内部 _bootstrap_capabilities 调用。现有 capability 字典访问全部改成 ProductOperationsCapabilities 字段访问。

- [ ] **Step 2: 在 E2E 入口显式发布 scripted 配置**

apps/worker/e2e.py 定义唯一测试规范：

```python
E2E_MODEL_SPEC = ModelConnectionProvisioningSpec(
    stable_key="product-scripted",
    backend_type=ModelBackendType.OPENAI,
    base_url="https://scripted.model.invalid/v1",
    model="product-scripted",
    non_secret_options={},
)
```

build_e2e_application 先调用 Provisioner，再把快照传给 build_product_application。

- [ ] **Step 3: 修改 Eval 与测试 fixture**

eval/fixtures.py 和 tests/agents/product_operations/conftest.py 各自在环境初始化阶段显式 provision 一次；单个 Run 或单次 advance 不重复发布。

- [ ] **Step 4: 删除组合根的数据库写入依赖**

composition/product_operations.py 不再 import Agent、AgentVersion、Skill、SkillVersion、ToolDefinitionRow、ToolVersionRow、ModelConnection 或 ModelConfigVersion 等配置持久化实体。

- [ ] **Step 5: 运行闭环测试**

Run:

```powershell
pytest tests/composition/test_product_operations_provisioning.py tests/agents/product_operations tests/apps/worker tests/eval -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add composition/product_operations.py apps/worker/e2e.py eval/fixtures.py tests/agents/product_operations/conftest.py tests/composition/test_product_operations_provisioning.py
git commit -m "refactor: make product composition read only"
```

### Task 4: 架构回归与 Provisioning P0 收口

**Files:**

- Modify: tests/architecture/test_dependency_boundaries.py
- Modify: docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md

- [ ] **Step 1: 增加静态边界测试**

断言：

- build_product_application 的 AST 中不存在 session.add；
- composition/product_operations.py 不导入配置持久化模型；
- scripted.model.invalid 只允许出现在 apps/worker/e2e.py、eval 或 tests；
- Provisioner 不导入 Runtime、AgentLoop 或 ToolExecutor。

- [ ] **Step 2: 运行目标测试**

Run:

```powershell
pytest tests/composition tests/architecture tests/agents/product_operations tests/apps/worker tests/eval -q
```

Expected: PASS。

- [ ] **Step 3: 运行静态检查**

Run:

```powershell
ruff check composition apps/worker eval tests/composition tests/architecture
mypy composition apps/worker eval
```

Expected: 两条命令均退出 0。

- [ ] **Step 4: 运行全量测试**

Run:

```powershell
pytest -q
```

Expected: PASS。

- [ ] **Step 5: 更新 ADR 状态**

只有本计划和 runtime-tool-binding-p0 计划都完成后，才把 ADR-002 更新为“P0 已实施，P1/P2 待实施”。

- [ ] **Step 6: 提交**

```powershell
git add tests/architecture/test_dependency_boundaries.py docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md
git commit -m "docs: close product provisioning p0"
```

