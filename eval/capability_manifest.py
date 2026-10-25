"""商品运营 Eval 的逻辑能力快照与命令行生成入口。

Manifest 只记录可跨隔离 Workspace 比较的 ``stable_key + version + hash``；
数据库 Version UUID 仍由每个 Run 自己固定，但不会被误当成跨 case 的能力身份。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from composition.provisioning.product_operations import ProductOperationsCapabilities
from eval.config import fixed_eval_config
from runtime.persistence.base import AccountStatus, ExecutionMode
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    BusinessPolicy,
    BusinessPolicyVersion,
    ModelConfigVersion,
    ModelConnection,
    Skill,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
)
from runtime.persistence.models.identity import Workspace


MANIFEST_SCHEMA_VERSION: Final = "1"
CAPABILITY_MANIFEST_MISMATCH: Final = "CAPABILITY_MANIFEST_MISMATCH"
DEFAULT_DATASET_DIR: Final = (
    Path(__file__).resolve().parent / "datasets" / "product_operations"
)
_DATABASE_URL_ENV: Final = "RUNTIME_TEST_DATABASE_URL"
_DEFAULT_DATABASE_URL: Final = (
    "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime_test"
)
_DATASET_SPLITS: Final = ("core", "clarification", "regression", "safety", "blind")


class CapabilityManifestMismatch(RuntimeError):
    """隔离环境发布的逻辑能力与已冻结 Manifest 不一致。"""

    code = CAPABILITY_MANIFEST_MISMATCH


class CapabilityVersionRef(BaseModel):
    """不含临时 UUID 的逻辑能力版本引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stable_key: str = Field(min_length=1)
    version: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)


class EvalCapabilityManifest(BaseModel):
    """一次 Eval 运行在发起任何模型请求前冻结的完整能力协议。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    agent: CapabilityVersionRef
    prompt_version: str = Field(min_length=1)
    skills: dict[str, CapabilityVersionRef]
    tools: dict[str, CapabilityVersionRef]
    model_snapshot: dict[str, Any]
    policies: dict[str, CapabilityVersionRef]
    dataset_hashes: dict[str, str]
    scorer_version: str = Field(min_length=1)
    capability_fingerprint: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_fingerprint(self) -> EvalCapabilityManifest:
        expected = capability_fingerprint(self.model_dump(mode="json"))
        if self.capability_fingerprint != expected:
            raise ValueError("capability_fingerprint does not match manifest content")
        if set(self.dataset_hashes) != set(_DATASET_SPLITS):
            raise ValueError("dataset_hashes must contain all five eval splits")
        return self


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def capability_fingerprint(document: Mapping[str, Any]) -> str:
    """计算 Manifest 自身以外全部字段的 canonical JSON SHA-256。"""
    payload = dict(document)
    payload.pop("capability_fingerprint", None)
    return _canonical_hash(payload)


def dataset_hashes(dataset_dir: Path = DEFAULT_DATASET_DIR) -> dict[str, str]:
    """按文件字节冻结五个数据分区。"""
    hashes: dict[str, str] = {}
    for split in _DATASET_SPLITS:
        path = dataset_dir / f"{split}.yaml"
        hashes[split] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


async def build_eval_capability_manifest(
    session_factory: Callable[[], AsyncSession],
    capabilities: ProductOperationsCapabilities,
    *,
    dataset_hashes_by_split: Mapping[str, str],
    scorer_version: str,
) -> EvalCapabilityManifest:
    """从 Provisioner 返回的精确行读取逻辑能力，不从 Python 常量拼版本。"""
    session = session_factory()
    try:
        agent_version, agent = (
            await session.execute(
                select(AgentVersion, Agent)
                .join(Agent, Agent.id == AgentVersion.agent_id)
                .where(AgentVersion.id == capabilities.agent_version_id)
            )
        ).one()
        skill_version, skill = (
            await session.execute(
                select(SkillVersion, Skill)
                .join(Skill, Skill.id == SkillVersion.skill_id)
                .where(SkillVersion.id == capabilities.skill_version_id)
            )
        ).one()
        tools: dict[str, CapabilityVersionRef] = {}
        for stable_key, version_id in capabilities.tool_version_ids.items():
            version, definition = (
                await session.execute(
                    select(ToolVersion, ToolDefinition)
                    .join(ToolDefinition, ToolDefinition.id == ToolVersion.tool_id)
                    .where(ToolVersion.id == version_id)
                )
            ).one()
            tool_hash = _canonical_hash(
                {
                    "stable_key": definition.stable_key,
                    "version": version.version,
                    "effect": version.effect.value,
                    "schema_hash": version.schema_hash,
                    "implementation_key": version.implementation_key,
                }
            )
            tools[stable_key] = CapabilityVersionRef(
                stable_key=definition.stable_key,
                version=version.version,
                content_hash=tool_hash,
            )
        model_version, connection = (
            await session.execute(
                select(ModelConfigVersion, ModelConnection)
                .join(
                    ModelConnection,
                    (ModelConnection.id == ModelConfigVersion.model_connection_id)
                    & (ModelConnection.workspace_id == ModelConfigVersion.workspace_id),
                )
                .where(ModelConfigVersion.id == capabilities.model_config_version_id)
            )
        ).one()
        policy_version, policy = (
            await session.execute(
                select(BusinessPolicyVersion, BusinessPolicy)
                .join(
                    BusinessPolicy,
                    (BusinessPolicy.id == BusinessPolicyVersion.business_policy_id)
                    & (BusinessPolicy.workspace_id == BusinessPolicyVersion.workspace_id),
                )
                .where(
                    BusinessPolicyVersion.id
                    == capabilities.pricing_policy_version_id
                )
            )
        ).one()
        document: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "agent": {
                "stable_key": agent.stable_key,
                "version": agent_version.version,
                "content_hash": agent_version.definition_hash,
            },
            "prompt_version": str(agent_version.definition_json["system_prompt_version"]),
            "skills": {
                skill.stable_key: {
                    "stable_key": skill.stable_key,
                    "version": skill_version.version,
                    "content_hash": skill_version.content_hash,
                }
            },
            "tools": {
                key: ref.model_dump(mode="json") for key, ref in sorted(tools.items())
            },
            "model_snapshot": {
                "stable_key": connection.stable_key,
                "version": model_version.version,
                "backend_type": model_version.backend_type.value,
                "base_url": model_version.base_url,
                "model": model_version.model,
                "non_secret_options": model_version.non_secret_options,
                "config_hash": model_version.config_hash,
            },
            "policies": {
                policy.stable_key: {
                    "stable_key": policy.stable_key,
                    "version": policy_version.version,
                    "content_hash": policy_version.config_hash,
                }
            },
            "dataset_hashes": dict(dataset_hashes_by_split),
            "scorer_version": scorer_version,
        }
        document["capability_fingerprint"] = capability_fingerprint(document)
        return EvalCapabilityManifest.model_validate(document)
    finally:
        await session.close()


async def require_manifest_match(
    session_factory: Callable[[], AsyncSession],
    capabilities: ProductOperationsCapabilities,
    expected: EvalCapabilityManifest,
) -> None:
    """在 Backend 首次调用前验证隔离 Workspace 的真实已发布行。"""
    actual = await build_eval_capability_manifest(
        session_factory,
        capabilities,
        dataset_hashes_by_split=expected.dataset_hashes,
        scorer_version=expected.scorer_version,
    )
    if actual.capability_fingerprint != expected.capability_fingerprint:
        raise CapabilityManifestMismatch(
            f"{CAPABILITY_MANIFEST_MISMATCH}: expected "
            f"{expected.capability_fingerprint}, got {actual.capability_fingerprint}"
        )


async def _generate_manifest(database_url: str) -> EvalCapabilityManifest:
    from composition.provisioning.product_operations import ProductOperationsProvisioner
    from eval.fixtures import EVAL_MODEL_SPEC, EVAL_PRICE_CONFIG

    engine = create_async_engine(database_url)
    workspace_id = uuid.uuid4()
    factory: Callable[[], AsyncSession] = lambda: AsyncSession(
        engine, expire_on_commit=False
    )
    session = factory()
    try:
        async with session.begin():
            session.add(
                Workspace(
                    id=workspace_id,
                    name=f"eval-manifest-{workspace_id.hex[:8]}",
                    status=AccountStatus.ACTIVE,
                    execution_mode=ExecutionMode.AUTO_EXECUTE,
                )
            )
    finally:
        await session.close()
    try:
        capabilities = await ProductOperationsProvisioner(factory).provision(
            workspace_id=workspace_id,
            model=EVAL_MODEL_SPEC,
            pricing_policy=EVAL_PRICE_CONFIG,
        )
        return await build_eval_capability_manifest(
            factory,
            capabilities,
            dataset_hashes_by_split=dataset_hashes(),
            scorer_version=fixed_eval_config().scorer_version,
        )
    finally:
        cleanup = factory()
        try:
            async with cleanup.begin():
                for table in (
                    "business_policy_versions",
                    "business_policies",
                    "model_config_versions",
                    "model_connections",
                ):
                    await cleanup.execute(
                        sa.text(f"DELETE FROM {table} WHERE workspace_id = :ws"),
                        {"ws": workspace_id},
                    )
                await cleanup.execute(
                    sa.text("DELETE FROM workspaces WHERE id = :ws"),
                    {"ws": workspace_id},
                )
        finally:
            await cleanup.close()
            await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成冻结的 EvalCapabilityManifest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    database_url = os.environ.get(_DATABASE_URL_ENV, _DEFAULT_DATABASE_URL)
    manifest = asyncio.run(_generate_manifest(database_url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = [
    "CAPABILITY_MANIFEST_MISMATCH",
    "CapabilityManifestMismatch",
    "CapabilityVersionRef",
    "EvalCapabilityManifest",
    "build_eval_capability_manifest",
    "capability_fingerprint",
    "dataset_hashes",
    "require_manifest_match",
]
