"""数据集加载:YAML/JSONL 场景文件 → ``list[Scenario]``(计划 05 Task 1,最小实现)。

文件格式:
- YAML:顶层为场景列表,或映射形式 ``{"schema_version": ..., "scenarios": [...]}``;
  映射形式声明 ``schema_version`` 时必须与 ``SCHEMA_VERSION`` 一致,否则拒绝
  (数据集与 Schema 版本共同参与协议指纹,防止静默漂移);
- JSONL:每行一个场景 JSON 对象,空行忽略(schema_version 不逐行声明);
- ``load_dataset`` 按后缀分派单文件,目录则按文件名排序加载全部受支持文件
  (``.yaml``/``.yml``/``.jsonl``),其余后缀报错;
- ``load_manifest`` 加载分区 manifest(:class:`DatasetManifest`,计划 05
  Task 5:四分区 ID 清单 + held_out 文件字节 SHA-256),schema_version
  漂移即拒绝。

错误约定:结构类型错误(顶层/scenarios 不是列表)抛 ``TypeError``;取值
错误(schema_version 漂移、不受支持的后缀)抛 ``ValueError``。
"""

import json
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from eval.schema import SCHEMA_VERSION, Scenario


_SUPPORTED_SUFFIXES: Final[frozenset[str]] = frozenset({".yaml", ".yml", ".jsonl"})


class DatasetManifest(BaseModel):
    """数据集分区 manifest:四类 ID 清单与 Held-out 文件哈希。

    ``held_out_sha256`` 是对 ``held_out.yaml`` 文件字节计算的 SHA-256
    十六进制摘要,正式评测前重算比对以检测保留集漂移。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    scorer_version: str
    development_ids: tuple[str, ...]
    regression_ids: tuple[str, ...]
    held_out_ids: tuple[str, ...]
    safety_ids: tuple[str, ...]
    held_out_sha256: str = Field(min_length=64, max_length=64)


def load_yaml(path: Path | str) -> list[Scenario]:
    """加载单个 YAML 数据集文件为场景列表。"""
    document: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, dict):
        declared = document.get("schema_version")
        if declared is not None and declared != SCHEMA_VERSION:
            raise ValueError(
                f"数据集 {path} 的 schema_version={declared!r} 与当前 "
                f"SCHEMA_VERSION={SCHEMA_VERSION} 不一致"
            )
        items = document.get("scenarios")
        if not isinstance(items, list):
            raise TypeError(f"YAML 数据集 {path} 的 scenarios 必须是场景列表")
    elif isinstance(document, list):
        items = document
    else:
        raise TypeError(
            f"YAML 数据集 {path} 顶层必须是场景列表或 {{schema_version, scenarios}}"
        )
    return [Scenario.model_validate(item) for item in items]


def load_jsonl(path: Path | str) -> list[Scenario]:
    """加载单个 JSONL 数据集文件为场景列表(每行一个场景,空行忽略)。"""
    scenarios: list[Scenario] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        scenarios.append(Scenario.model_validate(json.loads(line)))
    return scenarios


def load_manifest(path: Path | str) -> DatasetManifest:
    """加载数据集分区 manifest;``schema_version`` 漂移即拒绝。"""
    document: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    manifest = DatasetManifest.model_validate(document)
    if manifest.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"manifest {path} 的 schema_version={manifest.schema_version!r} 与当前 "
            f"SCHEMA_VERSION={SCHEMA_VERSION} 不一致"
        )
    return manifest


def load_dataset(source: Path | str) -> list[Scenario]:
    """按后缀分派加载数据集;目录则按文件名排序加载全部受支持文件。"""
    target = Path(source)
    if target.is_dir():
        scenarios: list[Scenario] = []
        for file in sorted(target.iterdir()):
            if file.is_file() and file.suffix.lower() in _SUPPORTED_SUFFIXES:
                scenarios.extend(load_dataset(file))
        return scenarios
    suffix = target.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return load_yaml(target)
    if suffix == ".jsonl":
        return load_jsonl(target)
    raise ValueError(f"unsupported dataset file suffix {suffix!r}: {target}")
