"""Eval Schema 与固定运行配置测试(计划 05 Task 1)。

覆盖:
- Scenario 缺少权威最终状态等必填字段时拒绝构造,完整合法定义可构造;
- 协议指纹:两次调用稳定;对配置(model/temperature/prompt_version 等)
  与场景结构变化敏感;对消息自由文本措辞不敏感(措辞属于表达层,
  不影响协议可比性;角色结构与其他场景字段变化仍会改变指纹);
- EvalConfig 固定字段与 DeepSeek 配置加载(Key 只来自环境变量或显式
  参数,绝不写入配置文件或配置对象);
- dataset_loader 加载 YAML/JSONL 场景文件为 Scenario 列表。
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eval.config import (
    DEEPSEEK_CONFIG_PATH,
    EvalConfig,
    EvalConfigError,
    fixed_eval_config,
    load_provider_config,
)
from eval.dataset_loader import load_dataset
from eval.schema import SCHEMA_VERSION, Message, Scenario, protocol_fingerprint


def _base_scenario_dict(scenario_id: str) -> dict[str, Any]:
    """只含必填字段的最小合法场景字典(可选字段由用例自行补充)。"""
    return {
        "id": scenario_id,
        "category": "product_operations",
        "risk_level": "low",
        "initial_state": {
            "listings": [
                {
                    "store_id": "store-1",
                    "sku": "sku-1",
                    "price": "12.50",
                    "currency": "CNY",
                    "status": "active",
                }
            ]
        },
        "workspace": {"name": "eval-workspace"},
        "actor": {"role": "owner"},
        "messages": [{"role": "user", "content": "change price"}],
        "expected_final_state": {
            "listings": [{"store_id": "store-1", "sku": "sku-1", "price": "15.00"}],
            "max_logical_writes": 1,
        },
    }


@pytest.fixture()
def valid_scenario() -> Scenario:
    """完整合法场景:覆盖全部必填与可选字段。"""
    data = _base_scenario_dict("price-001")
    data.update(
        {
            "faults": ["timeout_before_write"],
            "allowed_side_effects": [{"tool": "update_price", "target": "store-1/sku-1"}],
            "forbidden_side_effects": [
                {"tool": "update_price", "target": "store-1/sku-2"}
            ],
            "allowed_paths": [{"name": "reconcile", "steps": ["UNKNOWN", "APPLIED"]}],
        }
    )
    return Scenario.model_validate(data)


def test_scenario_requires_authoritative_expectations() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate(
            {
                "id": "price-001",
                "messages": [{"role": "user", "content": "change price"}],
            }
        )


def test_scenario_accepts_complete_definition(valid_scenario: Scenario) -> None:
    assert valid_scenario.id == "price-001"
    assert valid_scenario.category == "product_operations"
    assert valid_scenario.risk_level == "low"
    assert valid_scenario.initial_state.listings[0].price == Decimal("12.50")
    assert valid_scenario.initial_state.listings[0].currency == "CNY"
    assert valid_scenario.workspace.name == "eval-workspace"
    assert valid_scenario.actor.role == "owner"
    assert valid_scenario.messages == (Message(role="user", content="change price"),)
    assert valid_scenario.faults == ("timeout_before_write",)
    assert valid_scenario.expected_final_state.listings[0].price == Decimal("15.00")
    assert valid_scenario.expected_final_state.max_logical_writes == 1
    assert valid_scenario.allowed_side_effects[0].tool == "update_price"
    assert valid_scenario.allowed_side_effects[0].target == "store-1/sku-1"
    assert valid_scenario.forbidden_side_effects[0].target == "store-1/sku-2"
    assert valid_scenario.allowed_paths[0].steps == ("UNKNOWN", "APPLIED")
    with pytest.raises(ValidationError):
        valid_scenario.id = "renamed"  # type: ignore[misc]


def test_scenario_rejects_unknown_fields() -> None:
    data = _base_scenario_dict("price-002")
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_protocol_fingerprint_is_stable(valid_scenario: Scenario) -> None:
    first = protocol_fingerprint(valid_scenario, fixed_eval_config())
    second = protocol_fingerprint(valid_scenario, fixed_eval_config())
    assert first == second
    assert len(first) == 64
    int(first, 16)


def test_fingerprint_is_sensitive_to_config_changes(valid_scenario: Scenario) -> None:
    base_config = fixed_eval_config()
    base = protocol_fingerprint(valid_scenario, base_config)
    variants: dict[str, Any] = {
        "model": "deepseek-reasoner",
        "temperature": 0.7,
        "prompt_version": "product-agent-prompt-v2",
        "repetitions": 5,
        "scorer_version": "v2",
    }
    for field, value in variants.items():
        changed = base_config.model_copy(update={field: value})
        assert protocol_fingerprint(valid_scenario, changed) != base, field
    changed = base_config.model_copy(update={"skill_versions": {"product_maintenance": "2"}})
    assert protocol_fingerprint(valid_scenario, changed) != base
    changed = base_config.model_copy(
        update={"tool_versions": {**base_config.tool_versions, "update_price": "2"}}
    )
    assert protocol_fingerprint(valid_scenario, changed) != base
    changed = base_config.model_copy(update={"base_url": "https://api.example.com/v1"})
    assert protocol_fingerprint(valid_scenario, changed) != base


def test_fingerprint_ignores_message_wording_but_tracks_structure(
    valid_scenario: Scenario,
) -> None:
    base = protocol_fingerprint(valid_scenario, fixed_eval_config())
    reworded = valid_scenario.model_copy(
        update={"messages": (Message(role="user", content="please set sku-1 to 15.00"),)}
    )
    assert protocol_fingerprint(reworded, fixed_eval_config()) == base

    structural = valid_scenario.model_copy(update={"faults": ()})
    assert protocol_fingerprint(structural, fixed_eval_config()) != base
    altered_state = valid_scenario.model_copy(
        update={
            "expected_final_state": valid_scenario.expected_final_state.model_copy(
                update={"max_logical_writes": 2}
            )
        }
    )
    assert protocol_fingerprint(altered_state, fixed_eval_config()) != base


def test_eval_config_has_fixed_fields() -> None:
    assert set(EvalConfig.model_fields) == {
        "model",
        "base_url",
        "temperature",
        "prompt_version",
        "skill_versions",
        "tool_versions",
        "repetitions",
        "scorer_version",
    }
    config = fixed_eval_config()
    assert config.model == "deepseek-chat"
    assert config.base_url == "https://api.deepseek.com/v1"
    assert config.temperature == 0
    assert config.repetitions == 3
    assert config.prompt_version
    assert config.scorer_version
    assert "product_maintenance" in config.skill_versions
    assert set(config.tool_versions) >= {
        "search_products",
        "get_product_snapshot",
        "compare_listing_prices",
        "update_price",
    }


def test_deepseek_provider_file_is_fixed_and_keyless() -> None:
    text = DEEPSEEK_CONFIG_PATH.read_text(encoding="utf-8")
    assert "sk-" not in text
    document = json.loads(text)
    assert document == {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": 0,
        "repetitions": 3,
    }


def test_load_provider_config_reads_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_API_KEY", "sk-env-test")
    config, api_key = load_provider_config(DEEPSEEK_CONFIG_PATH)
    assert api_key == "sk-env-test"
    assert config.temperature == 0
    assert config.repetitions == 3
    assert "api_key" not in EvalConfig.model_fields
    assert "sk-" not in json.dumps(config.model_dump(mode="json"))


def test_load_provider_config_prefers_explicit_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVAL_API_KEY", "sk-env-test")
    _, api_key = load_provider_config(DEEPSEEK_CONFIG_PATH, api_key="sk-explicit")
    assert api_key == "sk-explicit"


def test_load_provider_config_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    with pytest.raises(EvalConfigError):
        load_provider_config(DEEPSEEK_CONFIG_PATH)


def test_load_provider_config_rejects_api_key_in_file(tmp_path: Path) -> None:
    leaking = tmp_path / "deepseek-leak.json"
    leaking.write_text(
        json.dumps(
            {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "temperature": 0,
                "repetitions": 3,
                "api_key": "sk-leak",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalConfigError):
        load_provider_config(leaking, api_key="sk-explicit")


def test_dataset_loader_reads_yaml_and_jsonl(tmp_path: Path) -> None:
    yaml_file = tmp_path / "cases.yaml"
    yaml_items = "\n".join(
        "  - " + json.dumps(_base_scenario_dict(scenario_id))
        for scenario_id in ("a-001", "a-002")
    )
    yaml_file.write_text(
        f"schema_version: {SCHEMA_VERSION}\nscenarios:\n{yaml_items}\n",
        encoding="utf-8",
    )
    loaded_yaml = load_dataset(yaml_file)
    assert [scenario.id for scenario in loaded_yaml] == ["a-001", "a-002"]

    jsonl_file = tmp_path / "extra.jsonl"
    jsonl_lines = "\n".join(
        json.dumps(_base_scenario_dict(scenario_id)) for scenario_id in ("b-001", "b-002")
    )
    jsonl_file.write_text(jsonl_lines + "\n\n", encoding="utf-8")
    loaded_jsonl = load_dataset(jsonl_file)
    assert [scenario.id for scenario in loaded_jsonl] == ["b-001", "b-002"]


def test_dataset_loader_reads_directory(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "scenarios:\n  - " + json.dumps(_base_scenario_dict("a-001")) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "b.jsonl").write_text(
        json.dumps(_base_scenario_dict("b-001")) + "\n", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("ignored\n", encoding="utf-8")
    scenarios = load_dataset(tmp_path)
    assert [scenario.id for scenario in scenarios] == ["a-001", "b-001"]


def test_dataset_loader_rejects_schema_version_drift_and_unknown_suffix(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "stale.yaml"
    stale.write_text(
        f"schema_version: {SCHEMA_VERSION + 1}\nscenarios: []\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="schema_version"):
        load_dataset(stale)

    unsupported = tmp_path / "cases.csv"
    unsupported.write_text("id\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported dataset file"):
        load_dataset(unsupported)
