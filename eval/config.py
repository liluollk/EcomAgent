"""固定 Eval 运行配置与 Provider 配置加载(计划 05 Task 1)。

- ``EvalConfig``:一次 Eval 运行的完整固定配置,全部字段参与协议指纹;
- ``fixed_eval_config``:全仓库唯一的固定运行配置,Provider 连接字段来自
  随库 ``eval/providers/deepseek.json``,版本字段与商品 Agent 实际发布版本
  对齐(提示词版本见 ``agents.product_operations.agent``);
- ``load_provider_config``:加载 Provider 配置并解析 API Key。Key 绝不落盘:
  显式参数优先,其次从环境变量 ``EVAL_API_KEY`` 读取,只随返回值交给调用方,
  不写入配置文件,也不写入 ``EvalConfig`` 对象;配置文件中出现任何 Key 字段
  立即拒绝。

DeepSeek 真实模型评测固定 ``temperature=0``、``repetitions=3``:配置文件或
加载结果偏离该固定值即拒绝,防止协议漂移。
"""

import json
import os
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError


API_KEY_ENV_VAR: Final[str] = "EVAL_API_KEY"
PROVIDERS_DIR: Final[Path] = Path(__file__).resolve().parent / "providers"
DEEPSEEK_CONFIG_PATH: Final[Path] = PROVIDERS_DIR / "deepseek.json"

_FIXED_PROMPT_VERSION: Final[str] = "product-agent-prompt-v1"
_FIXED_SKILL_VERSIONS: Final[dict[str, str]] = {"product_maintenance": "1"}
_FIXED_TOOL_VERSIONS: Final[dict[str, str]] = {
    "search_products": "1",
    "get_product_snapshot": "1",
    "compare_listing_prices": "1",
    "update_price": "1",
}
_FIXED_SCORER_VERSION: Final[str] = "v1"


class EvalConfigError(Exception):
    """Provider 配置非法、偏离固定值或缺少 API Key。"""


class EvalConfig(BaseModel):
    """一次 Eval 运行的完整固定配置;全部字段纳入协议指纹。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    prompt_version: str = Field(min_length=1)
    skill_versions: dict[str, str]
    tool_versions: dict[str, str]
    repetitions: int = Field(ge=1)
    scorer_version: str = Field(min_length=1)


class _ProviderFile(BaseModel):
    """Provider 连接配置文件的结构(禁止额外字段,含 api_key)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float
    repetitions: int = Field(ge=1)


def _load_provider_document(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalConfigError(f"无法读取 Provider 配置 {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise EvalConfigError(f"Provider 配置 {path} 顶层必须是 JSON 对象")
    return document


def _eval_config_from_document(path: Path, document: dict[str, object]) -> EvalConfig:
    try:
        provider = _ProviderFile.model_validate(document)
    except ValidationError as exc:
        raise EvalConfigError(
            f"Provider 配置 {path} 非法(配置文件不得包含 api_key 等额外字段): {exc}"
        ) from exc
    if provider.provider != "deepseek":
        raise EvalConfigError(f"不支持的 Provider: {provider.provider!r}")
    if provider.temperature != 0:
        raise EvalConfigError("DeepSeek 评测固定 temperature=0,拒绝偏离")
    if provider.repetitions != 3:
        raise EvalConfigError("DeepSeek 评测固定 repetitions=3,拒绝偏离")
    return EvalConfig(
        model=provider.model,
        base_url=provider.base_url,
        temperature=provider.temperature,
        prompt_version=_FIXED_PROMPT_VERSION,
        skill_versions=dict(_FIXED_SKILL_VERSIONS),
        tool_versions=dict(_FIXED_TOOL_VERSIONS),
        repetitions=provider.repetitions,
        scorer_version=_FIXED_SCORER_VERSION,
    )


def fixed_eval_config() -> EvalConfig:
    """返回全仓库唯一的固定运行配置(不含任何 API Key)。

    每次调用重新读取随库 ``deepseek.json`` 并校验固定值;文件静态入库,
    同一提交下结果恒定,保证协议指纹可比。
    """
    return _eval_config_from_document(DEEPSEEK_CONFIG_PATH, _load_provider_document(DEEPSEEK_CONFIG_PATH))


def load_provider_config(
    path: Path | str, *, api_key: str | None = None
) -> tuple[EvalConfig, str]:
    """加载 Provider 配置并解析 API Key,返回 ``(EvalConfig, api_key)``。

    Key 解析顺序:显式 ``api_key`` 参数优先,其次环境变量 ``EVAL_API_KEY``;
    两者皆缺失时抛出 ``EvalConfigError``。Key 只存在于返回值与调用方内存,
    绝不写入文件,也不出现在 ``EvalConfig`` 对象中。
    """
    config = _eval_config_from_document(Path(path), _load_provider_document(Path(path)))
    resolved = api_key if api_key is not None else os.environ.get(API_KEY_ENV_VAR)
    if not resolved:
        raise EvalConfigError(
            f"缺少 API Key:请显式传入或设置环境变量 {API_KEY_ENV_VAR}"
        )
    return config, resolved
