"""DeepSeek 产品 Agent 真实模型运行器(计划 05 Task 7)。

把固定配置的商品 Agent 评测端到端跑起来::

    python -m eval.product_suite --split development|regression|frozen_test \
        --output reports/xxx.json

流程(前置校验全部通过才发任何模型请求):

1. **数据集完整性**:加载分区 manifest;``frozen_test`` 先比对
   ``frozen_test.yaml`` 文件字节 SHA-256 与 ``manifest.frozen_test_sha256``
   (冻结集漂移即拒绝),再校验场景文件 ID 与 manifest 分区清单一致;
2. **环境变量预检**:`EVAL_BASE_URL`/`EVAL_MODEL`/`EVAL_API_KEY` 缺一即明确
   报错(逐个列出变量名)并以非零码退出;`EVAL_BASE_URL`/`EVAL_MODEL` 还须
   与随库固定 Provider 配置(``eval/providers/deepseek.json``)一致——偏离即
   协议漂移,拒绝运行;API Key 只留在内存,绝不写入文件/日志/报告;
3. **Provider 连通性探测**:轻量请求(``GET /models``,零 token 消耗),失败
   → INFRASTRUCTURE_ERROR 报告(仍写出 JSON)并以非零码退出;
4. **运行**:按 :class:`eval.config.EvalConfig` 固定 repetition(3)用
   :class:`eval.runner.EvalRunner` + 真实
   :class:`adapters.model_backends.openai_backend.OpenAIBackend` 跑完整集合
   (每 case/repetition 经 :func:`adapters.commerce.product_runtime.build_product_runtime`
   的生产装配路径);运行中不修改用例与 Scorer;
5. **报告**:COMPLETED 且计入质量分的结果经 ``score_case`` 判分,
   :class:`eval.reporting.json_report.ReportBuilder` 组装 JSON 报告(协议
   指纹/分项/基础设施错误/真实分母/脱敏轨迹引用)写出 ``--output``。

退出码:数据集/环境校验失败 → 2;基础设施错误或执行失败 → 1;全部用例
端到端执行完成 → 0(任务质量好坏由报告与 Task 8 的发布门槛判定,不在此
折算)。真实运行需要当前终端会话提供三个环境变量;Secret 不写入任何文件。

本模块禁止 import apps(与 eval 包其余部分同一约束);测试不经网络、不碰
数据库地覆盖前置校验路径。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import openai
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from adapters.model_backends.openai_backend import OpenAIBackend
from eval.config import (
    API_KEY_ENV_VAR,
    EvalConfig,
    EvalConfigError,
    fixed_eval_config,
)
from eval.dataset_loader import DatasetManifest, load_dataset, load_manifest
from eval.reporting.json_report import ReportBuilder
from eval.runner import EvalRunner
from eval.schema import SCHEMA_VERSION, Scenario, protocol_fingerprint
from eval.scorers.composite_result import score_case


PRODUCT_DATASET_DIR: Final[Path] = (
    Path(__file__).resolve().parent / "datasets" / "product_operations"
)

_BASE_URL_ENV: Final[str] = "EVAL_BASE_URL"
_MODEL_ENV: Final[str] = "EVAL_MODEL"
_REQUIRED_ENV_VARS: Final[tuple[str, ...]] = (_BASE_URL_ENV, _MODEL_ENV, API_KEY_ENV_VAR)

_SPLIT_FILES: Final[dict[str, str]] = {
    "development": "development.yaml",
    "regression": "regression.yaml",
    "frozen_test": "frozen_test.yaml",
}

_DATABASE_URL_ENV: Final[str] = "RUNTIME_TEST_DATABASE_URL"
_DEFAULT_DATABASE_URL: Final[str] = (
    "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime_test"
)

_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]


class ProductSuiteError(Exception):
    """数据集/配置校验失败:前置条件不满足,拒绝运行。"""


# ---------------------------------------------------------------------------
# 前置校验:数据集完整性与环境变量
# ---------------------------------------------------------------------------


def _load_split(
    split: str, dataset_dir: Path
) -> tuple[list[Scenario], DatasetManifest]:
    """加载分区场景并校验完整性;frozen_test 先比对冻结哈希。"""
    manifest = load_manifest(dataset_dir / "manifest.json")
    relative = _SPLIT_FILES.get(split)
    if relative is None:
        raise ProductSuiteError(f"unknown split {split!r}")
    path = dataset_dir / relative
    if not path.is_file():
        raise ProductSuiteError(f"dataset file is missing: {path}")
    if split == "frozen_test":
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != manifest.frozen_test_sha256:
            raise ProductSuiteError(
                f"frozen_test_sha256 mismatch for {path}: manifest declares "
                f"{manifest.frozen_test_sha256}, file hashes to {actual}; "
                "refusing to run against a drifted frozen set"
            )
    scenarios = load_dataset(path)
    if not scenarios:
        raise ProductSuiteError(f"dataset {path} declares no scenarios")
    declared_ids = sorted(
        {
            "development": manifest.development_ids,
            "regression": manifest.regression_ids,
            "frozen_test": manifest.frozen_test_ids,
        }[split]
    )
    actual_ids = sorted(scenario.id for scenario in scenarios)
    if actual_ids != declared_ids:
        raise ProductSuiteError(
            f"dataset {path} scenario ids do not match the {split} partition "
            f"declared in manifest.json"
        )
    return scenarios, manifest


def _missing_env_vars() -> list[str]:
    """缺失(空白即缺失)的必需环境变量名,按声明序返回。"""
    return [name for name in _REQUIRED_ENV_VARS if not (os.environ.get(name) or "").strip()]


def _resolve_run_config() -> tuple[EvalConfig, str]:
    """校验环境变量与固定配置的一致性,返回 (EvalConfig, api_key)。

    Key 只经返回值进入内存,不写入任何文件/报告;``EVAL_BASE_URL``/
    ``EVAL_MODEL`` 与随库固定 Provider 配置偏离即协议漂移,拒绝运行。
    """
    missing = _missing_env_vars()
    if missing:
        raise ProductSuiteError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + "; set them in the current shell session (the API key is only read "
            f"from {API_KEY_ENV_VAR} and never written to disk)"
        )
    config = fixed_eval_config()
    base_url = os.environ[_BASE_URL_ENV].strip()
    model = os.environ[_MODEL_ENV].strip()
    if base_url != config.base_url:
        raise ProductSuiteError(
            f"{_BASE_URL_ENV} ({base_url!r}) does not match the fixed provider "
            f"config base_url ({config.base_url!r}); refusing protocol drift"
        )
    if model != config.model:
        raise ProductSuiteError(
            f"{_MODEL_ENV} ({model!r}) does not match the fixed provider config "
            f"model ({config.model!r}); refusing protocol drift"
        )
    return config, os.environ[API_KEY_ENV_VAR]


# ---------------------------------------------------------------------------
# 协议指纹与判分
# ---------------------------------------------------------------------------


def run_protocol_fingerprint(scenarios: Sequence[Scenario], config: EvalConfig) -> str:
    """整轮协议指纹:canonical JSON(schema 版本 + 固定配置 + 各例指纹)的 SHA-256。

    单例指纹随场景结构而异;整轮可比性由"同集合 + 同配置"的派生指纹承载,
    与 :func:`eval.schema.protocol_fingerprint` 同构造方式。
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "config": config.model_dump(mode="json"),
        "scenario_fingerprints": sorted(
            protocol_fingerprint(scenario, config) for scenario in scenarios
        ),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 运行与报告
# ---------------------------------------------------------------------------


async def _probe_provider(client: openai.AsyncOpenAI) -> str | None:
    """轻量连通性探测(``GET /models``);失败返回错误描述(不抛出)。"""
    try:
        await client.models.list()
    except Exception as error:  # noqa: BLE001 - 探测失败统一归基础设施错误
        return f"provider probe failed: {type(error).__name__}: {error}"
    return None


async def _execute(
    engine: AsyncEngine,
    scenarios: list[Scenario],
    config: EvalConfig,
    api_key: str,
) -> tuple[dict[str, Any], bool]:
    """探测 → 固定 repetition 全集运行 → 判分 → 报告;返回 (报告, 是否干净)。"""
    client = openai.AsyncOpenAI(base_url=config.base_url, api_key=api_key)
    infrastructure_errors: list[dict[str, Any]] = []
    try:
        probe_error = await _probe_provider(client)
        if probe_error is not None:
            infrastructure_errors.append(
                {"case_id": None, "repetition": None, "error": probe_error}
            )
            report = ReportBuilder().build(
                [],
                protocol_fingerprint=run_protocol_fingerprint(scenarios, config),
                infrastructure_errors=infrastructure_errors,
                config=config,
            )
            return report, False

        runner = EvalRunner(
            engine,
            config=config,
            backend_factory=lambda scenario: OpenAIBackend(client, config.model),
            repetitions=config.repetitions,
        )
        round_result = await runner.run(scenarios)

        scenarios_by_id = {scenario.id: scenario for scenario in scenarios}
        scored = [
            score_case(
                scenarios_by_id[result.case_id],
                result,
                scorer_version=config.scorer_version,
            )
            for result in round_result.results
            if result.status == "COMPLETED" and result.included_in_quality_score
        ]
        report = ReportBuilder().build(
            [*round_result.results, *scored],
            protocol_fingerprint=run_protocol_fingerprint(scenarios, config),
            infrastructure_errors=infrastructure_errors,
            config=config,
        )
        clean = not (
            round_result.failed
            or round_result.infrastructure_errors
            or round_result.skipped
        )
        return report, clean
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.product_suite",
        description=(
            "Run the DeepSeek product agent eval suite and write a JSON report."
        ),
    )
    parser.add_argument(
        "--split",
        required=True,
        choices=tuple(_SPLIT_FILES),
        help="dataset partition to run",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="path of the JSON report to write (parent directories are created)",
    )
    parser.add_argument(
        "--dataset-dir",
        default=str(PRODUCT_DATASET_DIR),
        help="directory holding manifest.json and the partition YAML files",
    )
    args = parser.parse_args(argv)

    # 1) 数据集完整性(frozen_test 哈希先行)。
    try:
        scenarios, manifest = _load_split(args.split, Path(args.dataset_dir))
    except (ProductSuiteError, ValueError, TypeError, OSError) as error:
        return _fail(f"dataset validation failed: {error}")

    # 2) 环境变量预检与固定配置一致性。
    try:
        config, api_key = _resolve_run_config()
    except (ProductSuiteError, EvalConfigError) as error:
        return _fail(str(error))

    # 3) 探测 + 运行 + 报告(测试库准备与其余套件同一约定)。
    database_url = os.environ.get(_DATABASE_URL_ENV, _DEFAULT_DATABASE_URL)
    _prepare_database(database_url)
    engine = create_async_engine(database_url)
    try:
        report, clean = asyncio.run(_execute(engine, scenarios, config, api_key))
    finally:
        asyncio.run(engine.dispose())
    report["dataset"] = {
        "split": args.split,
        "dataset_dir": str(Path(args.dataset_dir)),
        "scenario_count": len(scenarios),
        "frozen_test_sha256_verified": args.split == "frozen_test",
        "manifest_scorer_version": manifest.scorer_version,
    }

    if args.output is not None:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    totals = report["totals"]
    print(
        f"completed={totals['completed']} failed={totals['failed']} "
        f"infrastructure_errors={totals['infrastructure_errors']} "
        f"skipped={totals['skipped']}"
    )
    print(f"protocol_fingerprint={report['protocol_fingerprint']}")
    metrics = report["metrics"]
    for name in (
        "task_completion_rate",
        "decision_accuracy",
        "dangerous_side_effect_rate",
        "false_rejection_rate",
        "recovery_success_rate",
        "duplicate_write_rate",
    ):
        print(f"{name}={_summary_rate(metrics[name])}")
    return 0 if clean else 1


def _summary_rate(value: Any) -> str:
    """CLI 摘要行比率:``None`` → ``n/a``。"""
    if value is None:
        return "n/a"
    if isinstance(value, int | float):
        return f"{value:.4f}"
    return str(value)


def _fail(message: str) -> int:
    """前置校验失败:明确报错(含缺失变量名/哈希)并以非零码退出。"""
    print(f"product_suite: {message}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# 测试库准备(与 eval.runtime_suite / eval.recovery_suite 同一约定)
# ---------------------------------------------------------------------------


def _database_name(database_url: str) -> str:
    return database_url.rsplit("/", 1)[-1].split("?", 1)[0]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = database_url
    return config


async def _create_database_if_missing(database_url: str, name: str) -> None:
    admin_url = database_url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            exists = await connection.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            )
            if exists.scalar() is None:
                await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin_engine.dispose()


def _prepare_database(database_url: str) -> None:
    """确保测试库存在并应用迁移(幂等、非破坏性;mock 平台属测试面)。"""
    name = _database_name(database_url)
    if "test" not in name:
        raise RuntimeError(
            "product suite refuses to run against a non-test database "
            f"({name!r}); set {_DATABASE_URL_ENV} to an *_test database"
        )
    asyncio.run(_create_database_if_missing(database_url, name))
    command.upgrade(_alembic_config(database_url), "head")


if __name__ == "__main__":
    raise SystemExit(main())
