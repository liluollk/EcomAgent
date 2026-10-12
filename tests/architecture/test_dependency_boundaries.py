import ast
import re
from pathlib import Path


FORBIDDEN = ("agents.", "adapters.", "apps.", "eval.")

RUNTIME_BUSINESS_TERMS = (
    "sku",
    "store_id",
    "listing",
    "price",
    "product_operations",
    "commerce",
    "refund",
)


def test_runtime_commands_are_not_nested_under_workspace() -> None:
    assert Path("runtime/commands/session.py").exists()
    assert not Path("runtime/workspace/session_commands.py").exists()


def test_runtime_source_has_no_product_domain_vocabulary() -> None:
    violations: list[str] = []
    pattern = re.compile(
        r"(?i)(?<![a-z0-9_])(" + "|".join(RUNTIME_BUSINESS_TERMS) + r")(?![a-z0-9_])"
    )
    for path in Path("runtime").rglob("*.py"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                violations.append(f"{path}:{line_number}: {line.strip()}")
    assert violations == []


def _called_names(method: ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_product_application_delegates_request_resume_commands() -> None:
    tree = ast.parse(
        Path("composition/product_operations.py").read_text(encoding="utf-8")
    )
    product_application = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductApplication"
    )
    methods = {
        node.name: node
        for node in product_application.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    forbidden_direct_services = {
        "InputService",
        "RunService",
        "ConfirmationService",
        "JobRepository",
    }
    for method_name in ("answer", "confirm", "reject_confirmation"):
        calls = _called_names(methods[method_name])
        assert "SessionCommandService" in calls
        assert calls.isdisjoint(forbidden_direct_services)


def test_runtime_does_not_import_upper_layers() -> None:
    violations: list[str] = []
    for path in Path("runtime").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for module in FORBIDDEN:
            if f"import {module}" in text or f"from {module}" in text:
                violations.append(f"{path}: {module}")
    assert violations == []


def test_product_composition_is_not_owned_by_an_adapter() -> None:
    assert not Path("adapters/commerce/product_runtime.py").exists()
    assert Path("composition/product_operations.py").exists()


def test_runtime_does_not_import_product_composition() -> None:
    violations = []
    for path in Path("runtime").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import composition." in text or "from composition." in text:
            violations.append(str(path))
    assert violations == []


def test_composition_may_import_runtime_agent_and_adapter_layers() -> None:
    """组合根是唯一允许同时依赖 Runtime、Agent 与 Adapter 的地方。"""
    text = Path("composition/product_operations.py").read_text(encoding="utf-8")
    assert "from runtime." in text
    assert "from agents." in text
    assert "from adapters." in text


def test_business_policy_does_not_import_composition() -> None:
    violations = []
    for path in Path("agents/product_operations/policy").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import composition." in text or "from composition." in text:
            violations.append(str(path))
    assert violations == []


def test_read_tool_handlers_do_not_dispatch_on_stable_key() -> None:
    """读 Tool Handler 一 Tool 一个,不得按 stable_key 在运行时分发。"""
    text = Path("agents/product_operations/tools/handlers.py").read_text(encoding="utf-8")
    assert "stable_key" not in text


def test_composition_carries_no_module_level_run_limits_constant() -> None:
    """运行限制只来自 AgentVersion 定义,组合根不保留模块级常量。"""
    text = Path("composition/product_operations.py").read_text(encoding="utf-8")
    assert "_LIMITS" not in text


def test_runtime_memory_has_no_product_specific_critical_kind() -> None:
    """执行关键事实类型使用通用名称,不再出现商品域 ``price_rule``。"""
    for path in Path("runtime").rglob("*.py"):
        assert "price_rule" not in path.read_text(encoding="utf-8"), str(path)


# ---------------------------------------------------------------------------
# Provisioning 与 Composition 边界(Provisioning P0)
# ---------------------------------------------------------------------------

_SCRIPTED_ENDPOINT_ALLOWLIST = {
    "apps/api/e2e.py",
    "apps/worker/e2e.py",
}


def _build_product_application_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(
        Path("composition/product_operations.py").read_text(encoding="utf-8")
    )
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "build_product_application"
    )


def test_build_product_application_never_writes_configuration() -> None:
    """普通组合根不得写库:AST 的调用集合中不含 session.add。"""
    assert "add" not in _called_names(_build_product_application_node())


def test_composition_does_not_import_configuration_persistence_models() -> None:
    """组合根不再依赖 Agent/Skill/Tool/Model 配置持久化模型(发布已外移)。"""
    text = Path("composition/product_operations.py").read_text(encoding="utf-8")
    assert "runtime.persistence.models.configuration" not in text


def test_scripted_model_endpoint_is_confined_to_e2e_entrypoints() -> None:
    """``scripted.model.invalid`` 只允许出现在显式 E2E 入口、eval 与 tests。"""
    offenders: list[str] = []
    for path in Path(".").rglob("*.py"):
        parts = path.parts
        if any(part in {"tests", "eval", ".venv", "node_modules"} for part in parts):
            continue
        if path.as_posix() in _SCRIPTED_ENDPOINT_ALLOWLIST:
            continue
        if "scripted.model.invalid" in path.read_text(encoding="utf-8"):
            offenders.append(path.as_posix())
    assert offenders == []


def test_provisioner_stays_out_of_the_execution_kernel() -> None:
    """Provisioner 只发布配置,不依赖 Runtime、AgentLoop 或 ToolExecutor。"""
    text = Path("composition/provisioning/product_operations.py").read_text(
        encoding="utf-8"
    )
    assert "runtime.orchestration" not in text
    assert "tool_executor" not in text
