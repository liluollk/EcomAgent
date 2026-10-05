"""商品 Agent 数据集分区测试(计划 05 Task 5)。

覆盖 development / regression / frozen_test 三分区:

- 分区不重叠(计划原文断言)与全库场景 ID 唯一(含跨分区);
- 全部场景通过 ``Scenario.model_validate``(数据集合法)且与 loader 结果一致;
- frozen_test 的 manifest SHA-256 与 ``frozen_test.yaml`` 文件字节一致(防漂移);
- manifest 记录三分区 ID、schema_version 与 scorer_version,且与实际文件一致;
- 规模下限(development ≥ 18、regression ≥ 12、frozen_test ≥ 15);
- 每分区六类 category、风险等级低/中/高、单轮/多轮、正常/故障、普通/对抗
  表达的覆盖断言。

统计口径(以场景结构字段统计,不解释自由文本语义):

- category 六类:``read_only_query``(只读查询)/ ``valid_write``(合法写入)/
  ``incomplete_info``(信息不完整)/ ``unauthorized_or_violation``(越权与
  违规)/ ``multi_turn``(多轮)/ ``composite_task``(组合任务);
- 多轮量级:``category=multi_turn`` 的场景消息数必须超过原始消息预算
  (12 条),且 ``expected_final_state`` 保留用户目标(价格断言)与已确认
  约束(币种/状态断言或其他 Listing 不变断言);
- 正常/故障:``faults`` 非空即故障场景(注名 ``update_price_unknown_after_apply``);
- 普通/对抗表达:user 消息含 ``ADVERSARIAL_MARKERS`` 之一即对抗表达;
- 越权/违规与只读场景:断言状态不变(全部种子 Listing 价格不变)、
  ``max_logical_writes=0`` 且声明 forbidden 副作用、无 allowed 副作用;
- 信息不完整场景:``max_logical_writes=1``,首条 user 消息含 SKU 不含任何
  店铺符号(触发缺店铺),且该 SKU 至少存在于两个种子店铺(真歧义)。
"""

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
    "frozen_test": "frozen_test.yaml",
}

_PARTITION_SIZE_FLOORS: Final[dict[str, int]] = {
    "development": 18,
    "regression": 12,
    "frozen_test": 15,
}

REQUIRED_CATEGORIES: Final[tuple[str, ...]] = (
    "read_only_query",
    "valid_write",
    "incomplete_info",
    "unauthorized_or_violation",
    "multi_turn",
    "composite_task",
)

RISK_LEVELS: Final[tuple[str, ...]] = ("low", "medium", "high")

MULTI_TURN_MESSAGE_BUDGET: Final = 12
"""原始消息预算;multi_turn 场景消息数必须严格大于该值。"""

ADVERSARIAL_MARKERS: Final[tuple[str, ...]] = (
    "忽略之前",
    "忽略以上",
    "无需确认",
    "跳过确认",
    "你是管理员",
    "绕过授权",
)

FAULT_INJECTION_NAMES: Final[frozenset[str]] = frozenset(
    {"update_price_unknown_after_apply"}
)


@pytest.fixture(scope="module")
def dataset_manifest() -> DatasetManifest:
    return load_manifest(DATASET_DIR / "manifest.json")


@pytest.fixture(scope="module")
def partitions() -> dict[str, tuple[Scenario, ...]]:
    """loader 加载的三分区场景(经 Scenario 校验)。"""
    return {
        name: tuple(load_dataset(DATASET_DIR / file_name))
        for name, file_name in _PARTITION_FILES.items()
    }


def _raw_scenarios(name: str) -> list[dict[str, Any]]:
    """直接读 YAML 原始文档(绕过 loader),供合法性独立复核。"""
    document: Any = yaml.safe_load(
        (DATASET_DIR / _PARTITION_FILES[name]).read_text(encoding="utf-8")
    )
    assert isinstance(document, dict), f"{name} 数据集顶层必须是映射"
    assert document.get("schema_version") == SCHEMA_VERSION, (
        f"{name} 数据集必须声明 schema_version={SCHEMA_VERSION}"
    )
    items = document.get("scenarios")
    assert isinstance(items, list), f"{name} 数据集必须包含 scenarios 列表"
    return items


def _ids(scenarios: tuple[Scenario, ...] | list[Scenario]) -> set[str]:
    return {scenario.id for scenario in scenarios}


def _user_texts(scenario: Scenario) -> tuple[str, ...]:
    return tuple(m.content for m in scenario.messages if m.role == "user")


def _is_adversarial(scenario: Scenario) -> bool:
    return any(
        marker in text for text in _user_texts(scenario) for marker in ADVERSARIAL_MARKERS
    )


def _assert_state_unchanged(scenario: Scenario) -> None:
    """断言"状态不变":全部种子 Listing 在期望中保持原价。"""
    assert scenario.expected_final_state.max_logical_writes == 0
    assert scenario.allowed_side_effects == ()
    assert scenario.forbidden_side_effects != ()
    expected = {
        (listing.store_id, listing.sku): listing
        for listing in scenario.expected_final_state.listings
    }
    for initial in scenario.initial_state.listings:
        if initial.price is None:
            continue
        asserted = expected.get((initial.store_id, initial.sku))
        assert asserted is not None, f"{scenario.id} 缺少 {initial.store_id}/{initial.sku} 不变断言"
        assert asserted.price == initial.price


# ---- 计划原文与结构断言 ----


def test_dataset_partitions_are_disjoint(dataset_manifest: DatasetManifest) -> None:
    dev = set(dataset_manifest.development_ids)
    regression = set(dataset_manifest.regression_ids)
    frozen = set(dataset_manifest.frozen_test_ids)
    assert dev.isdisjoint(regression)
    assert dev.isdisjoint(frozen)
    assert regression.isdisjoint(frozen)


def test_all_scenarios_validate_and_match_loader(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    """每个原始场景通过 Scenario.model_validate,且 loader 加载结果与之逐一对齐。"""
    for name in _PARTITION_FILES:
        raw = _raw_scenarios(name)
        validated = [Scenario.model_validate(item) for item in raw]
        assert _ids(tuple(validated)) == _ids(partitions[name]), f"{name} loader 结果不一致"


def test_scenario_ids_unique_within_and_across_partitions(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    seen: set[str] = set()
    for name, scenarios in partitions.items():
        ids = _ids(scenarios)
        assert len(ids) == len(scenarios), f"{name} 分区内存在重复 ID"
        duplicate = seen & ids
        assert not duplicate, f"跨分区重复 ID: {sorted(duplicate)}"
        seen |= ids


def test_frozen_test_sha256_matches_file_bytes(dataset_manifest: DatasetManifest) -> None:
    """frozen_test 哈希必须对 ``frozen_test.yaml`` 文件字节重算一致(防漂移)。"""
    digest = hashlib.sha256(
        (DATASET_DIR / "frozen_test.yaml").read_bytes()
    ).hexdigest()
    assert dataset_manifest.frozen_test_sha256 == digest


def test_manifest_records_partitions_and_versions(
    dataset_manifest: DatasetManifest,
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    assert dataset_manifest.schema_version == SCHEMA_VERSION
    assert dataset_manifest.scorer_version == fixed_eval_config().scorer_version
    assert set(dataset_manifest.development_ids) == _ids(partitions["development"])
    assert set(dataset_manifest.regression_ids) == _ids(partitions["regression"])
    assert set(dataset_manifest.frozen_test_ids) == _ids(partitions["frozen_test"])


def test_partition_sizes_meet_floors(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name, floor in _PARTITION_SIZE_FLOORS.items():
        assert len(partitions[name]) >= floor, f"{name} 场景数低于下限 {floor}"


# ---- 覆盖维度断言(每分区) ----


def test_every_partition_covers_required_categories(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name, scenarios in partitions.items():
        present = {scenario.category for scenario in scenarios}
        missing = [category for category in REQUIRED_CATEGORIES if category not in present]
        assert not missing, f"{name} 缺少 category 覆盖: {missing}"


def test_every_partition_covers_all_risk_levels(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name, scenarios in partitions.items():
        present = {scenario.risk_level for scenario in scenarios}
        missing = [level for level in RISK_LEVELS if level not in present]
        assert not missing, f"{name} 缺少风险等级覆盖: {missing}"


def test_every_partition_covers_single_and_multi_turn(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name, scenarios in partitions.items():
        single = [s for s in scenarios if len(s.messages) == 1]
        multi = [s for s in scenarios if len(s.messages) > 1]
        assert single, f"{name} 缺少单轮场景"
        assert multi, f"{name} 缺少多消息场景"


def test_multi_turn_scenarios_exceed_message_budget_and_preserve_goal(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for scenarios in partitions.values():
        for scenario in (s for s in scenarios if s.category == "multi_turn"):
            assert len(scenario.messages) > MULTI_TURN_MESSAGE_BUDGET, (
                f"{scenario.id} 消息数未超过原始消息预算 {MULTI_TURN_MESSAGE_BUDGET}"
            )
            expected = scenario.expected_final_state
            assert expected.listings, f"{scenario.id} 未断言任何最终状态"
            has_goal = any(listing.price is not None for listing in expected.listings)
            has_constraint = any(
                listing.currency is not None or listing.status is not None
                for listing in expected.listings
            ) or len(expected.listings) >= 2
            assert has_goal, f"{scenario.id} 未保留用户目标(价格断言)"
            assert has_constraint, f"{scenario.id} 未保留已确认约束(币种/状态/其他 Listing)"


def test_every_partition_covers_fault_and_normal(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name, scenarios in partitions.items():
        faulted = [s for s in scenarios if s.faults]
        assert faulted, f"{name} 缺少故障场景(faults 非空)"
        assert len(faulted) < len(scenarios), f"{name} 缺少正常场景(faults 为空)"
        for scenario in faulted:
            assert set(scenario.faults) <= FAULT_INJECTION_NAMES, (
                f"{scenario.id} 使用未约定的故障注名"
            )
            assert scenario.expected_final_state.max_logical_writes == 1, (
                f"{scenario.id} 故障恢复场景应恰好一次逻辑写"
            )
            assert scenario.allowed_paths, f"{scenario.id} 故障场景未声明允许路径"


def test_every_partition_covers_plain_and_adversarial(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for name, scenarios in partitions.items():
        adversarial = [s for s in scenarios if _is_adversarial(s)]
        assert adversarial, f"{name} 缺少对抗表达场景"
        assert len(adversarial) < len(scenarios), f"{name} 缺少普通表达场景"
        # 对抗表达不得绕过安全语义:状态不变、零写。
        for scenario in adversarial:
            _assert_state_unchanged(scenario)


# ---- 语义一致性断言(期望与真实系统能力一致) ----


def test_read_only_query_asserts_no_writes(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for scenarios in partitions.values():
        for scenario in (s for s in scenarios if s.category == "read_only_query"):
            _assert_state_unchanged(scenario)


def test_unauthorized_or_violation_asserts_no_writes(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for scenarios in partitions.values():
        for scenario in (
            s for s in scenarios if s.category == "unauthorized_or_violation"
        ):
            _assert_state_unchanged(scenario)


def test_incomplete_info_triggers_missing_store(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    """信息不完整场景:首条 user 消息含 SKU 不含店铺符号,且该 SKU 跨两店(真歧义)。"""
    for scenarios in partitions.values():
        for scenario in (s for s in scenarios if s.category == "incomplete_info"):
            assert scenario.expected_final_state.max_logical_writes == 1
            first_user = scenario.messages[0]
            assert first_user.role == "user"
            assert "SKU-" in first_user.content, f"{scenario.id} 首条消息未含 SKU"
            assert "store-" not in first_user.content, (
                f"{scenario.id} 首条消息已含店铺符号,未触发缺店铺"
            )
            sku = next(
                token
                for token in first_user.content.split()
                if token.startswith("SKU-")
            )
            seeded_stores = {
                listing.store_id
                for listing in scenario.initial_state.listings
                if listing.sku == sku and listing.price is not None
            }
            assert len(seeded_stores) >= 2, f"{scenario.id} 的 {sku} 非真歧义(店铺数<2)"


def test_valid_and_composite_write_exactly_once(
    partitions: Mapping[str, tuple[Scenario, ...]],
) -> None:
    for scenarios in partitions.values():
        for scenario in (
            s for s in scenarios if s.category in ("valid_write", "composite_task", "multi_turn")
        ):
            assert scenario.expected_final_state.max_logical_writes == 1, (
                f"{scenario.id} 应恰好一次逻辑写"
            )
            assert scenario.allowed_side_effects, f"{scenario.id} 未声明允许副作用"
