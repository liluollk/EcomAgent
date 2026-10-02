from pathlib import Path


FORBIDDEN = ("agents.", "adapters.", "apps.", "eval.")


def test_runtime_does_not_import_upper_layers() -> None:
    violations: list[str] = []
    for path in Path("runtime").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for module in FORBIDDEN:
            if f"import {module}" in text or f"from {module}" in text:
                violations.append(f"{path}: {module}")
    assert violations == []
