"""EvalCapabilityManifest 的冻结、完整性与失配拒绝测试。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.agent_tool_keys import P1_BUSINESS_TOOL_KEYS
from apps.worker.e2e import E2EScriptedBackend
from eval.capability_manifest import (
    CAPABILITY_MANIFEST_MISMATCH,
    CapabilityManifestMismatch,
    EvalCapabilityManifest,
    build_eval_capability_manifest,
    capability_fingerprint,
    dataset_hashes,
    require_manifest_match,
)
from eval.dataset_loader import load_dataset
from eval.fixtures import IsolatedEnvironment
from eval.results import CaseResult, FinalState
from eval.schema import InventoryState, Scenario
from eval.scorers.task_completion import TaskCompletionScorer
from runtime.capabilities.skill_activation import LOAD_SKILL_KEY
from runtime.persistence.base import ExecutionMode
from runtime.persistence.models.configuration import ToolDefinition, ToolVersion


def _changed_manifest(
    manifest: EvalCapabilityManifest, **updates: Any
) -> EvalCapabilityManifest:
    document = manifest.model_dump(mode="json")
    document.update(updates)
    document["capability_fingerprint"] = capability_fingerprint(document)
    return EvalCapabilityManifest.model_validate(document)


def test_manifest_contains_the_published_p1_capability_set_without_uuid(
    eval_manifest: EvalCapabilityManifest,
) -> None:
    assert set(eval_manifest.tools) == P1_BUSINESS_TOOL_KEYS | {LOAD_SKILL_KEY}
    assert set(eval_manifest.dataset_hashes) == {
        "core",
        "clarification",
        "regression",
        "safety",
        "blind",
    }
    document = eval_manifest.model_dump_json()
    assert "workspace_id" not in document
    assert "actor_id" not in document
    assert "version_id" not in document


def test_manifest_rejects_a_tampered_fingerprint(
    eval_manifest: EvalCapabilityManifest,
) -> None:
    document = eval_manifest.model_dump(mode="json")
    document["prompt_version"] = "tampered-prompt"
    with pytest.raises(ValidationError, match="capability_fingerprint"):
        EvalCapabilityManifest.model_validate(document)


async def test_manifest_mismatch_is_rejected_before_any_model_call(
    engine: AsyncEngine,
    valid_case: Scenario,
    eval_manifest: EvalCapabilityManifest,
) -> None:
    environment = await IsolatedEnvironment.create(
        engine,
        valid_case,
        backend=E2EScriptedBackend(),
        execution_mode=ExecutionMode.AUTO_EXECUTE,
    )
    try:
        mismatched = _changed_manifest(
            eval_manifest, prompt_version="product-operations-prompt-mismatch"
        )
        with pytest.raises(
            CapabilityManifestMismatch, match=CAPABILITY_MANIFEST_MISMATCH
        ):
            await require_manifest_match(
                lambda: AsyncSession(engine, expire_on_commit=False),
                environment.capabilities,
                mismatched,
            )
    finally:
        await environment.aclose()
        await environment.cleanup()


async def test_manifest_remains_frozen_when_a_new_tool_version_is_published(
    engine: AsyncEngine,
    valid_case: Scenario,
    eval_manifest: EvalCapabilityManifest,
) -> None:
    first = await IsolatedEnvironment.create(
        engine,
        valid_case,
        backend=E2EScriptedBackend(),
        execution_mode=ExecutionMode.AUTO_EXECUTE,
    )
    second: IsolatedEnvironment | None = None
    try:
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            async with session.begin():
                current, definition = (
                    await session.execute(
                        select(ToolVersion, ToolDefinition)
                        .join(ToolDefinition, ToolDefinition.id == ToolVersion.tool_id)
                        .where(
                            ToolDefinition.stable_key == "set_listing_price",
                            ToolVersion.version == 1,
                        )
                    )
                ).one()
                session.add(
                    ToolVersion(
                        id=uuid.uuid4(),
                        workspace_id=None,
                        tool_id=definition.id,
                        version=999,
                        effect=current.effect,
                        input_schema=current.input_schema,
                        output_schema=current.output_schema,
                        schema_hash=current.schema_hash,
                        implementation_key=current.implementation_key,
                    )
                )
        finally:
            await session.close()

        second = await IsolatedEnvironment.create(
            engine,
            valid_case,
            backend=E2EScriptedBackend(),
            manifest=eval_manifest,
            execution_mode=ExecutionMode.AUTO_EXECUTE,
        )
        rebuilt = await build_eval_capability_manifest(
            lambda: AsyncSession(engine, expire_on_commit=False),
            second.capabilities,
            dataset_hashes_by_split=dataset_hashes(),
            scorer_version=eval_manifest.scorer_version,
        )
        assert rebuilt.capability_fingerprint == eval_manifest.capability_fingerprint
        assert rebuilt.tools["set_listing_price"].version == 1
    finally:
        if second is not None:
            await second.aclose()
            await second.cleanup()
        await first.aclose()
        await first.cleanup()


async def test_inventory_gold_state_is_seeded_and_captured_authoritatively(
    engine: AsyncEngine,
) -> None:
    dataset = (
        Path(__file__).resolve().parents[2]
        / "eval"
        / "datasets"
        / "product_operations"
        / "core.yaml"
    )
    scenario = next(
        item for item in load_dataset(dataset) if item.id == "core-inventory-001"
    )
    environment = await IsolatedEnvironment.create(
        engine,
        scenario,
        backend=E2EScriptedBackend(),
        execution_mode=ExecutionMode.AUTO_EXECUTE,
    )
    try:
        captured = await environment.capture_final_state(logical_writes=0)
        assert captured.inventory_levels == scenario.initial_state.inventory_levels
    finally:
        await environment.aclose()
        await environment.cleanup()


def test_manifest_fingerprint_is_independent_of_temporary_database_ids(
    eval_manifest: EvalCapabilityManifest,
) -> None:
    rebuilt = EvalCapabilityManifest.model_validate(
        eval_manifest.model_dump(mode="json")
    )
    assert rebuilt.capability_fingerprint == eval_manifest.capability_fingerprint


def test_inventory_gold_fails_when_authoritative_quantity_does_not_match() -> None:
    dataset = (
        Path(__file__).resolve().parents[2]
        / "eval"
        / "datasets"
        / "product_operations"
        / "core.yaml"
    )
    scenario = next(
        item for item in load_dataset(dataset) if item.id == "core-inventory-001"
    )
    result = CaseResult(
        case_id=scenario.id,
        repetition=0,
        status="COMPLETED",
        included_in_quality_score=True,
        fingerprint="test",
        final_state=FinalState(
            listings=scenario.expected_final_state.listings,
            inventory_levels=(
                InventoryState(
                    store_id="store-inventory",
                    sku="SKU-8601",
                    location_ref="main-warehouse",
                    available_quantity=20,
                ),
            ),
            logical_writes=0,
        ),
    )
    score = TaskCompletionScorer(scenario).score(result)
    assert score.passed is False
    assert score.mismatches == (
        "store-inventory/SKU-8601/main-warehouse.available_quantity",
    )

