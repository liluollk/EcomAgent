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
