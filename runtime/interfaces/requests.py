"""Request 公共类型(架构文档 §9、§5、§6)。

架构文档在 PreToolUse 统一决策中定义了 REQUIRE_INPUT(InputRequirement) 与
REQUIRE_CONFIRMATION(ConfirmationSpec)。本模块给出两者的正式字段模型:

- ``InputRequirement``:InputRequirementResolver 把可由用户补充的问题转成的
  输入需求(架构文档 §5);Runtime 据此持久化 Input Request 并设定过期时间;
- ``ConfirmationSpec``:REQUIRE_CONFIRMATION 决策携带的确认规格,字段与确认
  指纹的绑定要素对应(架构文档 §6),供交互服务计算指纹并持久化。

向后兼容约束:PreToolUse 管线以无参形式构造两者(空骨架),因此全部字段
都带默认值;既有导出名不变。字段结构只描述"决策携带的数据",持久化列
(状态、过期时间、回答人等)由 ``runtime.persistence.models.interaction`` 定义。
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class InputRequirement(BaseModel):
    """REQUIRE_INPUT 决策携带的用户输入需求(架构文档 §5)。

    - ``field_paths``:需要补充的字段路径;回答只能写入这些路径,
      不能由前端替换整个 ToolCall 参数;
    - ``question``:问题文本;``response_schema``:回答的 JSON Schema;
    - ``options``:服务端生成的候选项。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_paths: tuple[str, ...] = ()
    question: str | None = None
    response_schema: Mapping[str, Any] = {}
    options: Mapping[str, Any] = {}


class ConfirmationSpec(BaseModel):
    """REQUIRE_CONFIRMATION 决策携带的确认规格(架构文档 §6)。

    字段与确认指纹的绑定要素一一对应;``normalized_arguments`` 必须是
    Input Normalization 之后的规范化参数,``resource_versions`` 是授权资源
    解析得到的 ``资源 ID → 版本`` 映射。Runtime 在创建 Confirmation Request
    时以这些要素计算 SHA-256 指纹(见 ``runtime.interaction.fingerprints``)。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID | None = None
    tool_name: str | None = None
    normalized_arguments: Mapping[str, Any] = {}
    resource_versions: Mapping[str, Any] = {}
    tool_schema_hash: str | None = None
    policy_version: int | None = None
    execution_mode_version: int | None = None


__all__ = ["ConfirmationSpec", "InputRequirement"]
