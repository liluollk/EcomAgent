"""Verify that runtime entry points are importable from the built wheel."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_wheel_imports.py <wheel-path>")

    wheel_path = Path(sys.argv[1]).resolve()
    if not wheel_path.is_file() or wheel_path.suffix != ".whl":
        raise SystemExit(f"wheel not found: {wheel_path}")

    sys.path.insert(0, str(wheel_path))
    for module_name in ("apps.assembly", "composition.product_operations"):
        module = importlib.import_module(module_name)
        module_path = str(Path(module.__file__).resolve())
        if not module_path.startswith(str(wheel_path)):
            raise SystemExit(f"{module_name} imported outside the wheel: {module_path}")
        print(f"{module_name}: {module_path}")


if __name__ == "__main__":
    main()
