"""规范化 JSON 指纹与确认指纹计算(计划 02 Task 7;架构文档《Runtime执行状态与恢复设计》§6)。

语义:
- ``canonical_json``:排序键、规范化数值(整数值浮点归一为整数、Decimal 归一、
  set 排序)后输出紧凑 JSON 文本,使同一种值的不同输入形态产生同一文本;
- 确认指纹 = ``SHA-256(绑定要素的规范化 JSON)``,绑定要素为:
  Actor、Workspace、ToolCall、operation_id、规范化参数摘要、资源版本、
  Tool Schema hash、Policy 版本、Execution Mode 版本;
- 参数以"规范化参数摘要"(``normalized_arguments_hash``)参与指纹——该摘要
  是 confirmation_requests 的实际列,verify 才能仅凭存储列与当前资源版本
  重算并比对(防 TOCTOU);
- 与架构文档 §6 公式相比,``tool_name`` 不单独参与指纹:ToolCall 已把调用
  钉死到具体 ToolCall,工具身份/版本漂移由 Tool Resolution 阶段与
  ``tool_schema_hash`` 检测(偏差已在任务范围内确认)。

``verify`` 重算指纹时对 ``tool_schema_hash`` 失败闭合:create 绑定了 hash 而
verify 未提供当前 hash 时视为已漂移;两者同为 None 时视为一致。
"""

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict


class ConfirmationBinding(BaseModel):
    """确认指纹绑定要素值对象(架构文档 §6)。

    ``normalized_arguments`` 为规范化参数(整对象参与);指纹内部先取其
    规范化 JSON 摘要,再与其余要素一起做规范化 JSON 后哈希。
    """

    model_config = ConfigDict(frozen=True)

    actor_id: uuid.UUID
    workspace_id: uuid.UUID
    tool_call_id: uuid.UUID
    operation_id: uuid.UUID | None = None
    normalized_arguments: Mapping[str, Any] = {}
    resource_versions: Mapping[str, Any] = {}
    tool_schema_hash: str | None = None
    policy_version: int | None = None
    execution_mode_version: int | None = None


_SHA256_HEX_LENGTH: Final = 64


def canonical_json(value: Any) -> str:
    """排序键、规范化数值后的紧凑 JSON 文本;对同一值保持确定性。"""
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonicalize(value: Any) -> Any:
    """递归规范化:数值归一、枚举/日期/UUID 转确定性字符串、set 排序。"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return f"non-finite:{value.hex()}"
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return str(value.normalize())
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(canonical_json(item) for item in value)
    return repr(value)


def sha256_hex(text: str) -> str:
    """UTF-8 编码后的 SHA-256 十六进制摘要(64 字符,匹配列宽 String(128))。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(digest) == _SHA256_HEX_LENGTH
    return digest


def normalized_arguments_digest(arguments: Mapping[str, Any]) -> str:
    """规范化参数摘要:规范化 JSON 后的 SHA-256(confirmation_requests 列)。"""
    return sha256_hex(canonical_json(dict(arguments)))


def fingerprint_from_parts(
    *,
    actor_id: uuid.UUID,
    workspace_id: uuid.UUID,
    tool_call_id: uuid.UUID,
    operation_id: uuid.UUID | None,
    normalized_arguments_hash: str,
    resource_versions: Mapping[str, Any],
    tool_schema_hash: str | None,
    policy_version: int | None,
    execution_mode_version: int | None,
) -> str:
    """由绑定要素(参数以摘要参与)计算确认指纹。

    ``verify`` 用当前资源版本替换 ``resource_versions`` 后重算本函数并与
    存储指纹比对:任一绑定要素变化都会改变指纹。
    """
    elements: dict[str, Any] = {
        "actor_id": str(actor_id),
        "workspace_id": str(workspace_id),
        "tool_call_id": str(tool_call_id),
        "operation_id": str(operation_id) if operation_id is not None else None,
        "normalized_arguments_hash": normalized_arguments_hash,
        "resource_versions": dict(resource_versions),
        "tool_schema_hash": tool_schema_hash,
        "policy_version": policy_version,
        "execution_mode_version": execution_mode_version,
    }
    return sha256_hex(canonical_json(elements))


def confirmation_fingerprint(binding: ConfirmationBinding) -> str:
    """由绑定要素值对象计算确认指纹(参数先取规范化 JSON 摘要)。"""
    return fingerprint_from_parts(
        actor_id=binding.actor_id,
        workspace_id=binding.workspace_id,
        tool_call_id=binding.tool_call_id,
        operation_id=binding.operation_id,
        normalized_arguments_hash=normalized_arguments_digest(binding.normalized_arguments),
        resource_versions=binding.resource_versions,
        tool_schema_hash=binding.tool_schema_hash,
        policy_version=binding.policy_version,
        execution_mode_version=binding.execution_mode_version,
    )


__all__ = [
    "ConfirmationBinding",
    "canonical_json",
    "confirmation_fingerprint",
    "fingerprint_from_parts",
    "normalized_arguments_digest",
    "sha256_hex",
]
