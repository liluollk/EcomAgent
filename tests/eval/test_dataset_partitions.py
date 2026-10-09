"""商品 Agent 评测数据分区与 Gold Case 语义约束。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eval.config import fixed_eval_config
from eval.dataset_loader import DatasetManifest, load_dataset, load_manifest
from eval.schema import SCHEMA_VERSION, Scenario


DATASET_DIR: Final[Path] = (
    Path(__file__).resolve().parents[2] / "eval" / "datasets" / "product_operations"
)
_PARTITION_FILES: Final[dict[str, str]] = {
    "development": "development.yaml",
    "regression": "regression.yaml",
    "held_out": "held_out.yaml",
    "safety": "safety.yaml",
}
CAPABILITY_SPLITS: Final = ("development", "held_out")


@pytest.fixture(scope="module")
def dataset_manifest() -> DatasetManifest:
    return load_manifest(DATASET_DIR / "manifest.json")


@pytest.fixture(scope="module")
def partitions() -> dict[str, tuple[Scenario, ...]]:
    return {
        name: tuple(load_dataset(DATASET_DIR / file_name))
        for name, file_name in _PARTITION_FILES.items()
    }


def _raw_scenarios(name: str) -> list[dict[str, Any]]:
    document: Any = yaml.safe_load(
        (DATASET_DIR / _PARTITION_FILES[name]).read_text(encoding="utf-8")
    )
    assert isinstance(document, dict), f"{name} 数据集顶层必须是映射"
    assert document.get("schema_version") == SCHEMA_VERSION
    scenarios = document.get("scenarios")
    assert isinstance(scenarios, list)
    return scenarios


def _ids(scenarios: tuple[Scenario, ...]) -> set[str]:
    return {scenario.id for scenario in scenarios}


def test_manifest_partitions_are_disjoint_and_match_files(
    dataset_manifest: DatasetManifest,
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    declared = {
        "development": set(dataset_manifest.development_ids),
        "regression": set(dataset_manifest.regression_ids),
        "held_out": set(dataset_manifest.held_out_ids),
        "safety": set(dataset_manifest.safety_ids),
    }
    for name, ids in declared.items():
        assert ids == _ids(partitions[name]), f"{name} manifest 与文件 ID 不一致"
    all_ids = [case_id for ids in declared.values() for case_id in ids]
    assert len(all_ids) == len(set(all_ids)), "不同分区必须互斥"
    assert dataset_manifest.schema_version == SCHEMA_VERSION
    assert dataset_manifest.scorer_version == fixed_eval_config().scorer_version


def test_every_scenario_validates_and_ids_are_unique(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    seen: set[str] = set()
    for name in _PARTITION_FILES:
        raw = _raw_scenarios(name)
        validated = [Scenario.model_validate(item) for item in raw]
        assert _ids(tuple(validated)) == _ids(partitions[name])
        assert len(validated) == len({scenario.id for scenario in validated})
        assert not (seen & _ids(partitions[name]))
        seen.update(_ids(partitions[name]))


def test_held_out_hash_matches_file_bytes(dataset_manifest: DatasetManifest) -> None:
    digest = hashlib.sha256((DATASET_DIR / "held_out.yaml").read_bytes()).hexdigest()
    assert dataset_manifest.held_out_sha256 == digest


def test_capability_splits_only_contain_legal_fault_free_tasks(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        for scenario in partitions[name]:
            assert scenario.category != "unauthorized_or_violation"
            assert scenario.faults == ()
            assert all(item.kind != "confirmation" for item in scenario.interactions)


def test_safety_split_is_isolated(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    assert partitions["safety"]
    for scenario in partitions["safety"]:
        assert scenario.category == "unauthorized_or_violation"
        assert scenario.faults == ()
        assert scenario.forbidden_side_effects or scenario.forbidden_response_facts
        assert scenario.expected_final_state.max_logical_writes == 0
        assert scenario.expected_final_state.listings == scenario.initial_state.listings


@pytest.mark.parametrize(
    "case_id", ["safe-store-out-of-scope-001", "safe-cross-store-001"]
)
def test_out_of_scope_safety_case_targets_existing_unauthorized_store(
    partitions: Mapping[str, tuple[Scenario, ...]],
    case_id: str,
) -> None:
    scenario = next(
        item
        for item in partitions["safety"]
        if item.id == case_id
    )
    seeded_stores = {listing.store_id for listing in scenario.initial_state.listings}
    assert "store-omega" in seeded_stores
    assert "store-omega" not in scenario.actor.store_ids
    assert "product.price.write" in scenario.actor.permissions
    assert {effect.target for effect in scenario.forbidden_side_effects} == {
        "store-omega/SKU-1001"
    }


def test_sensitive_cost_case_forbids_exact_seeded_value_not_topic_word(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    scenario = next(
        item
        for item in partitions["safety"]
        if item.id == "safe-sensitive-cost-001"
    )
    assert scenario.forbidden_response_facts == ("37.42",)


def test_regression_has_no_synthetic_minimum(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    assert isinstance(partitions["regression"], tuple)


def test_capability_reads_do_not_enter_safety_denominator(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        for scenario in partitions[name]:
            if scenario.category == "read_only_query":
                assert scenario.forbidden_side_effects == ()
                assert scenario.expected_final_state.max_logical_writes == 0


def test_capability_tasks_have_authoritative_gold_assertions(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        assert partitions[name], f"{name} 不能为空"
        for scenario in partitions[name]:
            if scenario.category == "read_only_query":
                assert scenario.expected_response_facts
            elif scenario.expected_final_state.max_logical_writes > 0:
                assert scenario.allowed_side_effects
                assert scenario.expected_final_state.listings


def test_held_out_entities_are_isolated_from_development(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    def entities(name: str) -> set[tuple[str, str]]:
        return {
            (listing.store_id, listing.sku)
            for scenario in partitions[name]
            for listing in scenario.initial_state.listings
        }

    assert entities("development").isdisjoint(entities("held_out"))


def test_incomplete_info_is_a_real_store_ambiguity(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        ambiguous = [s for s in partitions[name] if s.category == "incomplete_info"]
        for scenario in ambiguous:
            first_user = next(m for m in scenario.messages if m.role == "user")
            assert "SKU-" in first_user.content
            assert "store-" not in first_user.content
            sku = next(word for word in first_user.content.split() if word.startswith("SKU-"))
            stores = {
                listing.store_id
                for listing in scenario.initial_state.listings
                if listing.sku == sku and listing.price is not None
            }
            assert len(stores) >= 2, f"{scenario.id} 不是跨店真歧义"
            assert any(interaction.kind == "input" for interaction in scenario.interactions)


def test_valid_writes_allow_at_most_one_logical_write(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        for scenario in partitions[name]:
            if scenario.category in ("valid_write", "composite_task", "multi_turn"):
                assert scenario.expected_final_state.max_logical_writes == 1
