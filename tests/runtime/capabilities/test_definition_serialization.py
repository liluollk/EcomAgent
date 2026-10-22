"""能力定义的序列化稳定性:``definition_hash``/``content_hash`` 必须跨进程一致。

``AgentDefinition``/``SkillDefinition`` 用 ``frozenset`` 表达 Key 集合,
``frozenset`` 的迭代顺序随进程的字符串哈希种子变化。若直接 dump 成 JSON,
同一定义在不同进程会得到不同哈希:已发布版本的哈希看起来"自己变了",
冻结的 Eval 能力清单无法与运行时的真实行对上,版本也不可复现。

这里用 ``PYTHONHASHSEED`` 起子进程验证真实跨进程行为,而不是在单进程内
比较——单进程内比较不会暴露该问题。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from agents.product_operations.agent import PRODUCT_AGENT_DEFINITION
from agents.product_operations.skill import PRODUCT_MAINTENANCE_SKILL
from composition.provisioning.product_operations import _json_hash
from runtime.capabilities.registry import AgentDefinition, SkillDefinition


PROJECT_ROOT = Path(__file__).resolve().parents[3]

_HASH_SCRIPT = """
import json, sys
from {module} import {attribute} as definition
from composition.provisioning.product_operations import _json_hash

print(_json_hash(definition.model_dump(mode="json")))
"""


def _hash_in_subprocess(module: str, attribute: str, seed: str) -> str:
    """在指定 ``PYTHONHASHSEED`` 的子进程里计算定义哈希。"""
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = seed
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _HASH_SCRIPT.format(module=module, attribute=attribute),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("module", "attribute"),
    [
        ("agents.product_operations.agent", "PRODUCT_AGENT_DEFINITION"),
        ("agents.product_operations.skill", "PRODUCT_MAINTENANCE_SKILL"),
    ],
)
def test_definition_hash_is_stable_across_hash_seeds(module: str, attribute: str) -> None:
    hashes = {
        _hash_in_subprocess(module, attribute, seed) for seed in ("0", "1", "524287")
    }
    assert len(hashes) == 1, f"{module}.{attribute} 的哈希跨进程不稳定: {hashes}"


def test_agent_definition_serializes_key_sets_as_sorted_lists() -> None:
    payload = PRODUCT_AGENT_DEFINITION.model_dump(mode="json")
    for field in (
        "required_backend_capabilities",
        "required_skill_keys",
        "required_tool_keys",
        "required_source_keys",
    ):
        value = payload[field]
        assert isinstance(value, list), field
        assert value == sorted(value), field


def test_skill_definition_serializes_key_sets_as_sorted_lists() -> None:
    payload = PRODUCT_MAINTENANCE_SKILL.model_dump(mode="json")
    for field in ("required_tool_keys", "required_source_keys"):
        value = payload[field]
        assert isinstance(value, list), field
        assert value == sorted(value), field


def test_skill_definition_payload_survives_canonical_json_hash() -> None:
    """序列化结果必须能直接进 canonical JSON(不含 set 等非 JSON 类型)。"""
    payload: Mapping[str, object] = PRODUCT_MAINTENANCE_SKILL.model_dump(mode="json")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert len(text) > 0
    assert _json_hash(payload) == _json_hash(json.loads(text))


def test_definitions_accept_frozenset_inputs_unchanged() -> None:
    """序列化修复不改变 Python 侧契约:仍是 ``frozenset``,顺序无关。"""
    skill = SkillDefinition(
        name="示例",
        required_tool_keys=frozenset({"b", "a"}),
        required_source_keys=frozenset(),
    )
    dumped = skill.model_dump(mode="json")
    assert dumped["required_tool_keys"] == ["a", "b"]
    assert skill.required_tool_keys == frozenset({"a", "b"})

    agent = AgentDefinition(
        name="示例 Agent",
        required_backend_capabilities=frozenset({"c", "b", "a"}),
        required_skill_keys=frozenset(),
        required_tool_keys=frozenset({"z", "y"}),
        required_source_keys=frozenset(),
    )
    agent_payload = agent.model_dump(mode="json")
    assert agent_payload["required_backend_capabilities"] == ["a", "b", "c"]
    assert agent_payload["required_tool_keys"] == ["y", "z"]
