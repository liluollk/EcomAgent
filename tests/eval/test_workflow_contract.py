"""评测入口与 CI 工作流的契约测试。

真实模型评测在 CI 里只能按仓库给出的命令启动:`workflow_dispatch` 的分区
选项、必填参数与数据库前置,必须和 :mod:`eval.product_suite` 的实际接口一致。
这些用例不发网络请求、不连数据库,只做参数层校验——上一版正是因为在选项名
(已废弃的 ``development``/``held_out``)与缺失的 ``--capability-manifest``
上不一致,手动触发会在参数解析阶段就退出。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eval.product_suite import _SPLIT_FILES
from eval.product_suite import main as product_suite_main


WORKFLOW_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "eval.yml"
)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    document: Any = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "eval.yml 顶层必须是映射"
    return document


def _dispatch_options(workflow: dict[str, Any]) -> list[str]:
    # PyYAML 会把 ``on`` 解析成布尔 True(与 YAML 1.1 的 on/off 语义冲突)。
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    dispatch = triggers["workflow_dispatch"]
    return list(dispatch["inputs"]["split"]["options"])


def test_workflow_dispatch_options_match_product_suite_splits(
    workflow: dict[str, Any],
) -> None:
    """手动触发的分区选项 = ``release`` + ``product_suite --split`` 的合法取值。"""
    assert _dispatch_options(workflow) == ["release", *list(_SPLIT_FILES)]


def test_release_mode_runs_core_and_safety(workflow: dict[str, Any]) -> None:
    """发布验证必须同时跑 Core 和 Safety:Core 里没有安全适用用例。"""
    steps = workflow["jobs"]["product-real-model"]["steps"]
    named = {step.get("name"): step for step in steps}
    for name in ("Run core split", "Run safety split", "Run regression split"):
        assert name in named, f"缺少步骤 {name}"
        assert "release" in named[name]["if"]


def test_release_gate_receives_safety_report(workflow: dict[str, Any]) -> None:
    """门禁审查必须带 Safety 报告,否则安全项会被静默跳过。"""
    steps = workflow["jobs"]["product-real-model"]["steps"]
    joined = "\n".join(
        step["run"] for step in steps if isinstance(step.get("run"), str)
    )
    assert "--safety-report" in joined
    assert "eval.gates.check_release" in joined


def test_workflow_passes_required_capability_manifest(
    workflow: dict[str, Any],
) -> None:
    """工作流必须先生成能力清单,再把它传给 product_suite。"""
    steps = workflow["jobs"]["product-real-model"]["steps"]
    commands = [
        step["run"] for step in steps if isinstance(step.get("run"), str)
    ]
    joined = "\n".join(commands)
    assert "eval.capability_manifest" in joined
    assert "--capability-manifest" in joined


def test_workflow_declares_runtime_and_e2e_attestations(
    workflow: dict[str, Any],
) -> None:
    """发布 Gate 需要 Runtime/E2E 通过声明,工作流必须真的传入。"""
    steps = workflow["jobs"]["product-real-model"]["steps"]
    joined = "\n".join(
        step["run"] for step in steps if isinstance(step.get("run"), str)
    )
    assert "--runtime-suite-result" in joined
    assert "--e2e-result" in joined


def test_workflow_prepares_database_before_building_manifest(
    workflow: dict[str, Any],
) -> None:
    """能力清单要对着已迁移的库生成:迁移步骤必须早于清单步骤。"""
    steps = workflow["jobs"]["product-real-model"]["steps"]
    names = [step.get("name", "") for step in steps]
    migrate = next(
        index for index, name in enumerate(names) if "database" in name.lower()
    )
    manifest = next(
        index for index, name in enumerate(names) if "manifest" in name.lower()
    )
    assert migrate < manifest


def test_product_suite_cli_requires_capability_manifest() -> None:
    """缺 ``--capability-manifest`` 时参数解析即失败(不会带着空清单跑模型)。"""
    with pytest.raises(SystemExit) as exit_info:
        product_suite_main(["--split", "core"])
    assert exit_info.value.code == 2
