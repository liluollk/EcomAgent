"""ContextProjection 构建器:模型可见的结果投影(计划 02 Task 8;架构文档 §7)。

权威执行结果、模型投影与前端安全视图三者分离:

- 权威结果(:class:`~runtime.execution.tool_result_processor.ProcessedToolResult`
  的 ``authoritative_result``)保存原始数据(仅密钥类值脱敏),不做字段过滤与
  截断;超限走确定错误(无 Artifact Store);
- 模型投影(本模块 ``ContextProjection``)只包含 Output Schema 声明的字段,
  并按确定性上限裁剪(字段数/数组长度/字符串长度/嵌套深度),带
  ``truncated`` 标记与 ``content_hash``(规范化 JSON 的 SHA-256),同一输入
  永远得到同一投影;
- 前端安全视图由处理器基于同一投影组装,同样不含密钥与未声明字段。

裁剪策略是确定性的:字典按插入顺序保留前 N 个键,数组保留前 N 项,字符串
按码点截断到 N;任何裁剪发生即置 ``truncated=True``。本模块为纯逻辑,不触
达数据库,也不做脱敏(脱敏是处理器管线的固定阶段,先于投影构建)。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict


DEFAULT_MAX_FIELDS: Final = 32
"""模型投影单个对象保留的最大字段数。"""

DEFAULT_MAX_ARRAY_ITEMS: Final = 64
"""模型投影单个数组保留的最大项数。"""

DEFAULT_MAX_STRING_LENGTH: Final = 4096
"""模型投影单个字符串保留的最大码点数。"""

DEFAULT_MAX_DEPTH: Final = 8
"""模型投影的最大嵌套深度(超出部分替换为深度标记)。"""

_DEPTH_CUT_MARKER: Final = "…[DEPTH_LIMIT]"
"""超出深度上限时的占位文本(计入 truncated)。"""


@dataclass(frozen=True)
class ProjectionLimits:
    """投影确定性上限;注入便于测试与按 Tool 配置覆盖。"""

    max_fields: int = DEFAULT_MAX_FIELDS
    max_array_items: int = DEFAULT_MAX_ARRAY_ITEMS
    max_string_length: int = DEFAULT_MAX_STRING_LENGTH
    max_depth: int = DEFAULT_MAX_DEPTH


class ContextProjection(BaseModel):
    """不可变的模型投影:结构化数据 + 文本 + truncated 标记 + content_hash。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    structured_data: Mapping[str, Any]
    text: str | None
    truncated: bool
    content_hash: str


def content_hash(structured_data: Mapping[str, Any]) -> str:
    """规范化 JSON(排序键、UTF-8)的 SHA-256 十六进制摘要。"""
    canonical = json.dumps(structured_data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _truncate(value: Any, *, limits: ProjectionLimits, depth: int, flags: dict[str, bool]) -> Any:
    """递归裁剪;任何裁剪发生时置 ``flags["truncated"] = True``。"""
    if depth > limits.max_depth:
        flags["truncated"] = True
        return _DEPTH_CUT_MARKER
    if isinstance(value, Mapping):
        kept: dict[str, Any] = {}
        for index, key in enumerate(value):
            if index >= limits.max_fields:
                flags["truncated"] = True
                break
            kept[str(key)] = _truncate(value[key], limits=limits, depth=depth + 1, flags=flags)
        return kept
    if isinstance(value, list):
        kept_items: list[Any] = []
        for index, item in enumerate(value):
            if index >= limits.max_array_items:
                flags["truncated"] = True
                break
            kept_items.append(_truncate(item, limits=limits, depth=depth + 1, flags=flags))
        return kept_items
    if isinstance(value, str) and len(value) > limits.max_string_length:
        flags["truncated"] = True
        return value[: limits.max_string_length]
    return value


def build_context_projection(
    payload: Mapping[str, Any] | None,
    *,
    allowed_fields: frozenset[str] | None = None,
    limits: ProjectionLimits | None = None,
) -> ContextProjection:
    """构建模型投影:未声明字段过滤 → 确定性上限裁剪 → truncated/content_hash。

    - ``allowed_fields`` 非 None 时,顶层仅保留 Output Schema ``properties``
      声明的字段(被丢弃即置 ``truncated``);
    - ``payload`` 为 None(如 UNKNOWN 结果)时投影为空对象;
    - 投影是确定性的:同一输入与限制参数永远产出相同结果与 content_hash。
    """
    effective_limits = limits if limits is not None else ProjectionLimits()
    flags = {"truncated": False}
    data: dict[str, Any] = dict(payload) if payload is not None else {}
    if allowed_fields is not None:
        dropped = [key for key in data if key not in allowed_fields]
        for key in dropped:
            del data[key]
        if dropped:
            flags["truncated"] = True
    truncated_value = _truncate(data, limits=effective_limits, depth=1, flags=flags)
    truncated_data: dict[str, Any] = truncated_value
    return ContextProjection(
        structured_data=truncated_data,
        text=json.dumps(truncated_data, ensure_ascii=False, sort_keys=True, default=str),
        truncated=flags["truncated"],
        content_hash=content_hash(truncated_data),
    )


__all__ = [
    "DEFAULT_MAX_ARRAY_ITEMS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FIELDS",
    "DEFAULT_MAX_STRING_LENGTH",
    "ContextProjection",
    "ProjectionLimits",
    "build_context_projection",
    "content_hash",
]
