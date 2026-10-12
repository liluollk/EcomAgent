# Runtime Tool 绑定边界 P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在不增加新业务场景的前提下，把 PreToolUse 扩展和写执行元数据从商品级/Runtime 级全局配置收敛到单个 ToolBinding，并保持既有改价、确认与恢复行为。

**Architecture:** 固定 PreToolUse 阶段顺序仍由 Runtime 管理，每个 ToolBinding 提供自己的 PreToolProfile。写执行元数据先静态绑定到具体写 Tool，Runtime 和 AgentLoop 不再持有全局 adapter_key；P1/P2 再在这一接缝上增加库存、上下架和按店铺动态路由。

**Tech Stack:** Python 3.12、Pydantic、SQLAlchemy Async、PostgreSQL、pytest、pytest-asyncio、Ruff、mypy

---

## 范围与约束

本计划只实施 ADR-002 的 P0：

- 不新增库存或上下架 Tool；
- 不实现动态插件；
- 不修改 ADR-001 已接受但尚未实施的 Skill 模型；
- 不修改真实模型评测结果和简历正文；
- 不删除现有持久化 stable_key；
- 每个提交必须保持现有数据库迁移测试和改价闭环可运行。

## 文件结构

**新建：**

- runtime/execution/pretool/profiles.py：PreToolProfile、显式空实现和启动校验。
- runtime/execution/bindings.py：ToolExecutionProfile 写执行值对象。
- agents/product_operations/input/profiles.py：商品各 Tool 的 PreToolProfile 工厂。
- agents/product_operations/tools/handlers.py：独立读 Tool Handler。
- agents/product_operations/policy/price_pretool.py：改价 PreToolUse 业务策略。
- tests/runtime/execution/test_pretool_profiles.py：Runtime 层 Profile 校验测试。
- tests/agents/product_operations/test_tool_profiles.py：商品 Tool 参数、资源和策略绑定测试。

**修改：**

- runtime/capabilities/registry.py：ToolBinding 增加 pretool_registration 与 execution_profile。
- runtime/orchestration/agent_loop.py：ToolPin 携带写执行 Profile，删除全局 Adapter 能力字段。
- runtime/orchestration/runtime.py：删除全局 Adapter 能力构造参数。
- runtime/execution/tool_executor.py：接收单次 Tool 的 ToolExecutionProfile。
- composition/product_operations.py：显式注册每个 Tool 的 Handler/Profile，移除读 Tool 分发和全局 Adapter 配置。
- agents/product_operations/input/normalizer.py：从固定价格字段白名单改成实例化字段集合。
- agents/product_operations/input/requirements.py：ProductPreToolNormalizer 接收当前 Tool 的字段集合。
- tests/runtime/orchestration/test_agent_loop.py：按 ToolPin 注入写执行 Profile。
- tests/runtime/integration/test_crash_recovery.py：按 ToolPin 注入写执行 Profile。
- tests/agents/product_operations/test_read_tools.py：通过真实 Runtime 路径验证 query/store_ids 未丢失。
- tests/agents/product_operations/test_price_flow.py：保持确认、执行和回查行为。
- tests/architecture/test_dependency_boundaries.py：锁定 Runtime 不依赖商品模块。

### Task 1: 用失败测试锁定当前参数硬编码问题

**Files:**

- Modify: tests/agents/product_operations/test_read_tools.py
- Create: tests/agents/product_operations/test_tool_profiles.py

- [ ] **Step 1: 增加 search_products 的 Runtime 路径测试**

测试必须通过 ProductApplication 启动 Run，而不是直接调用 search_products 函数。脚本 Backend 生成：

```python
BackendToolCall(
    stable_key="search_products",
    arguments={
        "query": "跑鞋",
        "store_ids": [str(store_a_id), str(store_b_id)],
    },
)
```

断言最新 ToolCall.normalized_arguments 完整保留：

```python
assert snapshot.normalized_arguments == {
    "query": "跑鞋",
    "store_ids": [str(store_a_id), str(store_b_id)],
}
```

- [ ] **Step 2: 增加 compare_listing_prices 的多店铺参数测试**

```python
assert snapshot.normalized_arguments == {
    "sku": "SKU-001",
    "store_ids": [str(store_a_id), str(store_b_id)],
}
```

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
pytest tests/agents/product_operations/test_read_tools.py tests/agents/product_operations/test_tool_profiles.py -q
```

Expected: 新增测试失败，失败证据显示 query 或 store_ids 在 ProductPreToolNormalizer 后丢失；已有测试保持原状态。

- [ ] **Step 4: 提交测试**

```powershell
git add tests/agents/product_operations/test_read_tools.py tests/agents/product_operations/test_tool_profiles.py
git commit -m "test: expose per-tool normalization gap"
```

### Task 2: 引入 PreToolProfile 与启动期 fail-closed 校验

**Files:**

- Create: runtime/execution/pretool/profiles.py
- Create: runtime/execution/bindings.py
- Create: tests/runtime/execution/test_pretool_profiles.py
- Modify: runtime/capabilities/registry.py
- Modify: runtime/capabilities/validation.py

- [ ] **Step 1: 为 Profile 校验编写失败测试**

覆盖：

1. READ Tool 显式绑定 NoBusinessPolicy 可以注册；
2. WRITE Tool 缺少 business_policy 时拒绝；
3. WRITE Tool 缺少 resource_resolver 时拒绝；
4. WRITE Tool 缺少 execution_profile 时拒绝；
5. 未注册 Profile 不允许依赖 Pipeline 的隐式 None 默认值。

```python
def test_write_binding_requires_business_policy() -> None:
    with pytest.raises(AssemblyError, match="business_policy"):
        validate_tool_binding(write_binding_without_policy)
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
pytest tests/runtime/execution/test_pretool_profiles.py -q
```

Expected: FAIL，因为 profiles.py 和校验函数尚不存在。

- [ ] **Step 3: 实现最小 Profile 类型**

profiles.py 定义：

```python
@dataclass(frozen=True)
class PreToolProfile:
    input_normalizer: InputNormalizer
    argument_provenance_resolver: ArgumentProvenanceResolver
    input_requirement_resolver: InputRequirementResolver
    authorization_resource_resolver: AuthorizationResourceResolver
    business_policy: BusinessPolicy


class NoBusinessPolicy:
    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        return PolicyDecision(allowed=True)
```

NoBusinessPolicy 只能被显式创建，不能成为 WRITE Tool 的默认策略。

请求级上下文与工厂协议同时定义在 profiles.py：

```python
@dataclass(frozen=True)
class PreToolProfileContext:
    session: AsyncSession
    actor: ActorContext
    request: ToolCallRequest


class PreToolProfileFactory(Protocol):
    async def create(
        self,
        context: PreToolProfileContext,
    ) -> PreToolProfile: ...
```

- [ ] **Step 4: 扩展 ToolBinding**

```python
class ToolBinding(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    definition: ToolDefinition
    handler: Any
    pretool_registration: PreToolProfileRegistration
    execution_profile: ToolExecutionProfile | None = None
```

注册描述和值对象定义为：

```python
@dataclass(frozen=True)
class PreToolProfileRegistration:
    factory: PreToolProfileFactory
    policy_mode: Literal["NONE", "BUSINESS"]
    resource_mode: Literal["WORKSPACE", "RESOURCE"]
```

ToolExecutionProfile 定义为不可变值对象：

```python
class ToolExecutionProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    adapter_key: str
    supports_native_idempotency: bool = False
    supports_reconciliation: bool = False
```

- [ ] **Step 5: 增加启动校验**

校验错误使用稳定错误码：

- TOOL_PRETOOL_PROFILE_MISSING
- WRITE_TOOL_POLICY_MISSING
- WRITE_TOOL_RESOURCE_RESOLVER_MISSING
- WRITE_TOOL_EXECUTION_PROFILE_MISSING
- READ_TOOL_EXECUTION_PROFILE_FORBIDDEN

- [ ] **Step 6: 运行测试**

Run:

```powershell
pytest tests/runtime/execution/test_pretool_profiles.py tests/runtime/capabilities/test_assembly.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add runtime/execution/pretool/profiles.py runtime/execution/bindings.py runtime/capabilities/registry.py runtime/capabilities/validation.py tests/runtime/execution/test_pretool_profiles.py
git commit -m "refactor: bind pretool policy to each tool"
```

### Task 3: 为商品 Tool 建立独立参数与策略 Profile

**Files:**

- Modify: agents/product_operations/input/normalizer.py
- Modify: agents/product_operations/input/requirements.py
- Create: agents/product_operations/input/profiles.py
- Create: agents/product_operations/policy/price_pretool.py
- Modify: tests/agents/product_operations/test_tool_profiles.py

- [ ] **Step 1: 将 ProductPreToolNormalizer 改为显式字段集合**

```python
class ProductPreToolNormalizer:
    def __init__(
        self,
        requirement_resolver: ProductInputRequirementResolver,
        *,
        fields: tuple[str, ...],
        user_message: str,
        actor: ActorContext,
        answered: Mapping[str, str],
    ) -> None:
        self._fields = fields
```

normalize 只遍历 self._fields，不再读取全局 KEY_ARGUMENT_FIELDS。

字段值的 Decimal、UUID 和列表规范化继续复用 normalizer.py 中的纯函数；requirements.py 不再从全局 KEY_ARGUMENT_FIELDS 推导所有 Tool 的允许字段。

- [ ] **Step 2: 为现有 Tool 声明字段**

```python
TOOL_ARGUMENT_FIELDS = {
    "search_products": ("query", "store_ids"),
    "get_product_snapshot": ("store_id", "sku"),
    "compare_listing_prices": ("sku", "store_ids"),
    "update_price": ("store_id", "sku", "target_price"),
}
```

- [ ] **Step 3: 为 Tool 绑定独立资源解析器与策略**

规则：

- search_products：Workspace 查询权限，NoBusinessPolicy；
- get_product_snapshot：单 store_id 资源，NoBusinessPolicy；
- compare_listing_prices：多个 store_ids 资源，NoBusinessPolicy；
- update_price：单 store_id 资源，PriceBusinessPolicy；
- load_skill：Runtime Control Profile，不使用商品业务扩展。

把 composition/product_operations.py 中的 _PriceBusinessPolicy 移到 agents/product_operations/policy/price_pretool.py，并重命名为 PricePreToolPolicy。它仍然通过请求级 Session 和 Actor 调用现有 PricePolicy，不把 Repository 移入 Runtime。

- [ ] **Step 4: 测试新增写 Tool 漏配策略时确定性失败**

构造一个测试 WRITE Tool，故意复用 READ Profile，断言 assembly.validate() 返回 WRITE_TOOL_POLICY_MISSING。

- [ ] **Step 5: 运行测试**

Run:

```powershell
pytest tests/agents/product_operations/test_tool_profiles.py tests/agents/product_operations/test_argument_provenance.py tests/agents/product_operations/test_safety_matrix.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add agents/product_operations/input/normalizer.py agents/product_operations/input/requirements.py agents/product_operations/input/profiles.py agents/product_operations/policy/price_pretool.py composition/product_operations.py tests/agents/product_operations/test_tool_profiles.py
git commit -m "refactor: define product pretool profiles per tool"
```

### Task 4: PreToolUse 按当前 ToolBinding 获取 Profile

**Files:**

- Modify: runtime/execution/pretool/pipeline.py
- Modify: runtime/execution/tool_registry.py
- Modify: composition/product_operations.py
- Modify: tests/runtime/execution/test_pretool_pipeline.py

- [ ] **Step 1: 增加 Profile 解析测试**

断言同一个 RuntimeToolRegistry 中：

- search_products 使用 SearchProductsProfile；
- update_price 使用 UpdatePriceProfile；
- 未绑定 Profile 的 Tool 在 tool_resolution 后确定性 DENY。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
pytest tests/runtime/execution/test_pretool_pipeline.py -q
```

Expected: 新测试 FAIL。

- [ ] **Step 3: 调整 Pipeline 依赖**

PreToolDependencies 只保留 Runtime 统一依赖：

```python
@dataclass(frozen=True)
class PreToolDependencies:
    tool_registry: RuntimeToolRegistry
    schema_validator: RuntimeSchemaValidator
    authorizer: Authorizer
    execution_mode_resolver: ExecutionModeResolver
    accepted_confirmation_lookup: AcceptedConfirmationLookup
```

tool_resolution 成功后，从 ToolResolution.binding.pretool_registration 取得注册描述，再用本次评估上下文创建业务协作对象。固定十阶段顺序不改变。

- [ ] **Step 4: 删除 _ScopedProductPipeline 的全局商品扩展注入**

_ScopedProductPipeline 只构造 Runtime 依赖与本次 actor/context。商品 Profile 在 ToolBinding 注册时创建，涉及当前 Session/Actor 的协作对象通过工厂延迟创建，不在应用启动时持有请求级 Session。

工厂协议：

```python
class PreToolProfileFactory(Protocol):
    async def create(self, context: PreToolProfileContext) -> PreToolProfile: ...
```

启动期校验只读取 PreToolProfileRegistration.policy_mode 和 resource_mode；运行期 factory 才能读取请求级 Session、Actor 和 ToolCall。不得在 ToolBinding 中保存长生命周期数据库 Session。

PreToolDependencies 增加 profile_context 字段；Pipeline 在 tool_resolution 后只创建一次 Profile，并缓存在本次 PreToolContext 中，后续阶段复用同一实例。

- [ ] **Step 5: 运行测试**

Run:

```powershell
pytest tests/runtime/execution/test_pretool_pipeline.py tests/agents/product_operations/test_read_tools.py tests/agents/product_operations/test_price_policy.py -q
```

Expected: PASS；Task 1 暴露的 query/store_ids 丢失问题已修复。

- [ ] **Step 6: 提交**

```powershell
git add runtime/execution/pretool/pipeline.py runtime/execution/tool_registry.py composition/product_operations.py tests/runtime/execution/test_pretool_pipeline.py tests/agents/product_operations/test_read_tools.py
git commit -m "refactor: resolve pretool behavior from tool binding"
```

### Task 5: 把写执行元数据从 Runtime 移到 ToolPin

**Files:**

- Modify: runtime/orchestration/agent_loop.py
- Modify: runtime/orchestration/runtime.py
- Modify: runtime/execution/tool_executor.py
- Modify: composition/product_operations.py
- Modify: tests/runtime/orchestration/test_agent_loop.py
- Modify: tests/runtime/integration/test_crash_recovery.py

- [ ] **Step 1: 增加两个写 Tool 使用不同 Adapter Profile 的失败测试**

同一个 AgentLoop 注册两个 WRITE Tool：

```python
ToolExecutionProfile(
    adapter_key="pricing-mock",
    supports_native_idempotency=False,
    supports_reconciliation=True,
)

ToolExecutionProfile(
    adapter_key="inventory-rest",
    supports_native_idempotency=True,
    supports_reconciliation=True,
)
```

分别执行后断言 Operation.adapter_key 和能力字段来自各自 ToolPin，不来自 Runtime。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
pytest tests/runtime/orchestration/test_agent_loop.py -q
```

Expected: FAIL，因为 AgentLoop 仍使用全局 Adapter 字段。

- [ ] **Step 3: 扩展 ToolPin**

```python
@dataclass(frozen=True)
class ToolPin:
    stable_key: str
    version: int
    version_id: UUID
    effect: ToolEffect
    handler: Any | None = None
    output_schema: Mapping[str, Any] | None = None
    execution_profile: ToolExecutionProfile | None = None
```

- [ ] **Step 4: 删除 Runtime 和 AgentLoop 的全局字段**

从 Runtime.__init__、Runtime._build_loop 和 AgentLoop.__init__ 删除：

- adapter_key
- supports_native_idempotency
- supports_reconciliation

- [ ] **Step 5: 写操作只读取 pin.execution_profile**

_handle_require_confirmation 创建 PREPARED Operation 和 _execute_write 创建 ToolExecutor 时，都使用同一 pin.execution_profile。缺失 Profile 返回稳定失败码 WRITE_EXECUTION_PROFILE_MISSING。

- [ ] **Step 6: ToolExecutor 接收 Profile 值对象**

```python
def __init__(
    self,
    ...,
    execution_profile: ToolExecutionProfile,
) -> None:
    self._execution_profile = execution_profile
```

OperationService.begin 使用 Profile 中的字段。

- [ ] **Step 7: 运行 Runtime 测试**

Run:

```powershell
pytest tests/runtime/orchestration/test_agent_loop.py tests/runtime/integration/test_crash_recovery.py tests/runtime/execution/test_operations.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```powershell
git add runtime/orchestration/agent_loop.py runtime/orchestration/runtime.py runtime/execution/tool_executor.py composition/product_operations.py tests/runtime/orchestration/test_agent_loop.py tests/runtime/integration/test_crash_recovery.py
git commit -m "refactor: scope write execution profile to tool"
```

### Task 6: 拆除读 Tool 集中分发

**Files:**

- Create: agents/product_operations/tools/handlers.py
- Modify: composition/product_operations.py
- Modify: tests/agents/product_operations/test_read_tools.py

- [ ] **Step 1: 为三个独立 Handler 编写契约测试**

```python
handlers = (
    SearchProductsHandler(...),
    GetProductSnapshotHandler(...),
    CompareListingPricesHandler(...),
)

for handler in handlers:
    assert callable(handler.read)
```

每个 Handler 只接受自己 Tool 的参数，不读取 stable_key 做分发。

- [ ] **Step 2: 实现独立 Handler**

建立：

- SearchProductsHandler
- GetProductSnapshotHandler
- CompareListingPricesHandler

它们共享 SessionFactory 和 ActorContextFactory，但不共享 if/elif dispatcher。

- [ ] **Step 3: 从组合根删除 _ProductReadToolHandler 和 _store_ids_of**

注册表改为显式映射：

```python
handlers = {
    SEARCH_PRODUCTS_DEFINITION.stable_key: SearchProductsHandler(...),
    GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key: GetProductSnapshotHandler(...),
    COMPARE_LISTING_PRICES_DEFINITION.stable_key: CompareListingPricesHandler(...),
    UPDATE_PRICE_DEFINITION.stable_key: write_handler,
}
```

- [ ] **Step 4: 运行商品 Agent 测试**

Run:

```powershell
pytest tests/agents/product_operations -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add agents/product_operations/tools/handlers.py composition/product_operations.py tests/agents/product_operations/test_read_tools.py
git commit -m "refactor: split product read tool handlers"
```

### Task 7: 持久化确认资源版本，移除进程内版本字典

**Files:**

- Modify: agents/product_operations/policy/price_pretool.py
- Modify: composition/product_operations.py
- Modify: runtime/interaction/confirmation_service.py
- Modify: tests/agents/product_operations/test_price_flow.py
- Modify: tests/runtime/interaction/test_confirmation_requests.py

- [ ] **Step 1: 增加进程重启后的版本冲突测试**

测试流程：

1. update_price 进入 WAITING_CONFIRMATION；
2. 销毁 ProductOperationsApplication；
3. 修改 Listing.version；
4. 重建应用；
5. 接受确认并恢复 Run；
6. 断言写操作在外部调用前以 LISTING_VERSION_CONFLICT 拒绝。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
pytest tests/agents/product_operations/test_price_flow.py -k version_conflict_after_restart -q
```

Expected: FAIL，因为当前版本绑定保存在进程内 dict。

- [ ] **Step 3: 由 PriceBusinessPolicy 返回资源版本**

资源版本进入 ConfirmationSpec.resource_versions：

```python
{
    f"listing:{listing.id}": listing.version,
}
```

恢复重评时从已接受 ConfirmationRequest 读取原始版本并与当前 Listing.version 比较。

- [ ] **Step 4: 删除 _version_bindings**

从 _ProductPipelineFactory、_ScopedProductPipeline 和 _PriceBusinessPolicy 删除进程内字典。

- [ ] **Step 5: 运行确认与价格流程测试**

Run:

```powershell
pytest tests/runtime/interaction/test_confirmation_requests.py tests/agents/product_operations/test_price_flow.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add agents/product_operations/policy/price_pretool.py composition/product_operations.py runtime/interaction/confirmation_service.py tests/agents/product_operations/test_price_flow.py tests/runtime/interaction/test_confirmation_requests.py
git commit -m "fix: persist confirmed listing version"
```

### Task 8: 架构回归与 P0 收口

**Files:**

- Modify: tests/architecture/test_dependency_boundaries.py
- Modify: docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md

- [ ] **Step 1: 增加依赖边界断言**

断言：

- runtime 不导入 agents.product_operations；
- runtime 不导入 adapters.commerce；
- composition 可以同时导入 Runtime、Agent 和 Adapter；
- business policy 不导入 composition；
- read handler 不按 stable_key 分发。

- [ ] **Step 2: 运行分层测试**

Run:

```powershell
pytest tests/architecture tests/runtime tests/agents/product_operations tests/adapters/commerce -q
```

Expected: PASS。

- [ ] **Step 3: 运行静态检查**

Run:

```powershell
ruff check runtime agents adapters composition tests
mypy runtime agents adapters composition
```

Expected: 两条命令均退出 0；若仓库已有基线问题，只允许记录与本次文件无关的既有错误，不得用全局 ignore 掩盖。

- [ ] **Step 4: 运行全量测试**

Run:

```powershell
pytest -q
```

Expected: PASS。

- [ ] **Step 5: 更新 ADR 状态**

仅当 Task 1-8 全部通过后，把 ADR-002 的状态从“已接受，尚未实施”更新为“P0 已实施，P1/P2 待实施”，并在文档记录实际提交号和验证命令。

- [ ] **Step 6: 提交**

```powershell
git add tests/architecture/test_dependency_boundaries.py docs/架构/ADR-002-商品运营模块化单体与平台适配边界.md
git commit -m "docs: close runtime tool binding p0"
```

## P0 完成后的下一步

P0 验收通过后再分别制定：

1. ProductOperations 命名与 catalog/pricing/inventory/listing 模块迁移计划；
2. PricingGateway、InventoryGateway、ListingGateway 与 CapabilityRouter 计划；
3. 库存、上下架业务闭环和新的真实模型评测计划。

旧评测数字不能直接证明新业务模块或新平台路由有效；P1/P2 完成后必须重新执行对应评测。
