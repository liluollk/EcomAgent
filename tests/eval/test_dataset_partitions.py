"""商品 Agent 评测数据分区与 Gold Case 语义约束。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eval.config import fixed_eval_config
from eval.dataset_loader import DatasetManifest, load_dataset, load_manifest
from eval.schema import SCHEMA_VERSION, Scenario
from eval.scorers.fact_match import fact_in_text


DATASET_DIR: Final[Path] = (
    Path(__file__).resolve().parents[2] / "eval" / "datasets" / "product_operations"
)
_PARTITION_FILES: Final[dict[str, str]] = {
    "core": "core.yaml",
    "blind": "blind.yaml",
    "clarification": "clarification.yaml",
    "regression": "regression.yaml",
    "safety": "safety.yaml",
}
CAPABILITY_SPLITS: Final = ("core", "blind")
CORE_CATEGORY_COUNTS: Final = {
    "read_only_query": 4,
    "direct_write": 6,
    "relative_change": 6,
    "multi_store_decision": 6,
    "composite_task": 4,
    "multi_turn": 4,
    "inventory_adjustment": 1,
    "listing_publication": 1,
}


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
        "core": set(dataset_manifest.core_ids),
        "blind": set(dataset_manifest.blind_ids),
        "clarification": set(dataset_manifest.clarification_ids),
        "regression": set(dataset_manifest.regression_ids),
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


def test_frozen_hashes_match_file_bytes(dataset_manifest: DatasetManifest) -> None:
    core_digest = hashlib.sha256((DATASET_DIR / "core.yaml").read_bytes()).hexdigest()
    blind_digest = hashlib.sha256((DATASET_DIR / "blind.yaml").read_bytes()).hexdigest()
    assert dataset_manifest.core_sha256 == core_digest
    assert dataset_manifest.blind_sha256 == blind_digest


def test_core_and_blind_have_fixed_sizes_and_task_distribution(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    assert len(partitions["core"]) == 32
    assert len(partitions["blind"]) == 10
    assert len(partitions["clarification"]) == 6
    actual_counts = {
        category: sum(item.category == category for item in partitions["core"])
        for category in CORE_CATEGORY_COUNTS
    }
    assert actual_counts == CORE_CATEGORY_COUNTS


def test_capability_splits_only_contain_legal_fault_free_tasks(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        for scenario in partitions[name]:
            assert scenario.category != "unauthorized_or_violation"
            assert scenario.faults == ()
            assert all(item.kind != "confirmation" for item in scenario.interactions)


def test_core_has_no_interactions_and_clarification_uses_input_requests(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    assert all(not scenario.interactions for scenario in partitions["core"])
    for scenario in partitions["clarification"]:
        assert scenario.category == "incomplete_info"
        assert scenario.faults == ()
        assert any(item.kind == "input" for item in scenario.interactions)


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
        assert (
            scenario.expected_final_state.inventory_levels
            == scenario.initial_state.inventory_levels
        )


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


def test_blind_entities_are_isolated_from_core(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    def entities(name: str) -> set[tuple[str, str]]:
        return {
            (listing.store_id, listing.sku)
            for scenario in partitions[name]
            for listing in scenario.initial_state.listings
        }

    assert entities("core").isdisjoint(entities("blind"))


def test_incomplete_info_is_a_real_store_ambiguity(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    store_ambiguity = [
        scenario
        for scenario in partitions["clarification"]
        if scenario.id.startswith("clarify-store-")
    ]
    assert store_ambiguity
    for scenario in store_ambiguity:
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


def test_valid_writes_allow_at_most_one_logical_write(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name in CAPABILITY_SPLITS:
        for scenario in partitions[name]:
            if scenario.category != "read_only_query":
                assert scenario.expected_final_state.max_logical_writes == 1


def test_core_multi_store_writes_name_every_candidate_store(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    """选择规则可以由模型计算，但允许写入的候选店铺必须由用户明确给出。"""
    for scenario in partitions["core"]:
        if scenario.category != "multi_store_decision":
            continue
        user_text = " ".join(
            message.content for message in scenario.messages if message.role == "user"
        )
        candidate_stores = {
            listing.store_id for listing in scenario.initial_state.listings
        }
        assert all(store_id in user_text for store_id in candidate_stores), scenario.id


def test_expected_facts_are_derivable_from_declared_authoritative_states(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    """期望事实必须能从场景声明的权威状态里得到证据。

    否则该样本必定失败:模型说声明的值,权威状态里没有对应证据(判 unsupported);
    模型说权威状态里的值,又不符合期望事实(判 omitted)。这条校验专门拦截
    "Gold 期望与判分口径互相矛盾"的样本(如状态声明 ``PUBLISHED`` 却要求回答
    ``ACTIVE``)。
    """
    for name, scenarios in partitions.items():
        for scenario in scenarios:
            declared = json.dumps(
                {
                    "initial": scenario.initial_state.model_dump(mode="json"),
                    "expected_final": scenario.expected_final_state.model_dump(
                        mode="json"
                    ),
                },
                ensure_ascii=False,
            )
            for fact in scenario.expected_response_facts:
                assert fact_in_text(fact.value, declared), (
                    f"{name}/{scenario.id}: 期望事实 {fact.value!r} "
                    f"(source={fact.source}) 在声明的权威状态里找不到证据"
                )
