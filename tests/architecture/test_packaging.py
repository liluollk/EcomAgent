import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_composition_is_included_in_wheel_and_container_build() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "composition" in wheel_packages
    assert "COPY composition ./composition" in dockerfile
