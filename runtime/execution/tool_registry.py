"""运行期 Tool 查询:capabilities.ToolRegistry 的薄封装(计划 02 Task 6)。

供 PreToolUse 管线的 tool_resolution 阶段按 ``stable_key`` + ``version``
解析 Run 已固定的 Tool 版本及其进程内 Binding。本模块不做业务决策、不查
数据库;未知 Tool、版本漂移与禁用(未分类 effect)版本以显式状态标记,
由管线统一拒绝。
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from runtime.capabilities.registry import ToolBinding, ToolRegistry, ToolVersionRecord
from runtime.interfaces.tools import ToolDefinition


TOOL_RESOLVED: Final = "RESOLVED"
TOOL_UNKNOWN: Final = "UNKNOWN_TOOL"
TOOL_VERSION_MISMATCH: Final = "VERSION_MISMATCH"
TOOL_DISABLED: Final = "TOOL_DISABLED"

ToolResolutionStatus = Literal["RESOLVED", "UNKNOWN_TOOL", "VERSION_MISMATCH", "TOOL_DISABLED"]


class ToolResolution(BaseModel):
    """tool_resolution 阶段的查询结果值对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ToolResolutionStatus
    record: ToolVersionRecord | None = None
    binding: ToolBinding | None = None


class RuntimeToolRegistry:
    """按 stable_key + version 的运行期 Tool 查询视图。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def resolve(self, *, stable_key: str, version: int) -> ToolResolution:
        """解析该 Run 期望的精确 Tool 版本;漂移与禁用版本不通过。"""
        record = self._registry.find_by_key_version(stable_key, version)
        if record is None:
            # 区分"从未注册"与"注册了其他版本":前者未知,后者版本漂移。
            current = self._registry.find_by_key(stable_key)
            if current is None:
                return ToolResolution(status=TOOL_UNKNOWN)
            return ToolResolution(status=TOOL_VERSION_MISMATCH, record=current)
        if record.effect is None:
            # 未经本地 effect/permission 分类的 MCP Tool 默认禁用。
            return ToolResolution(status=TOOL_DISABLED, record=record)
        return ToolResolution(
            status=TOOL_RESOLVED,
            record=record,
            binding=self._registry.binding_for_version(record.version_id),
        )

    def definition_for(self, stable_key: str) -> ToolDefinition | None:
        """返回该 Tool 当前版本的模型可见数据定义(来自进程内 ToolBinding)。"""
        binding = self._registry.binding_for_key(stable_key)
        return binding.definition if binding is not None else None


__all__ = [
    "TOOL_DISABLED",
    "TOOL_RESOLVED",
    "TOOL_UNKNOWN",
    "TOOL_VERSION_MISMATCH",
    "RuntimeToolRegistry",
    "ToolResolution",
    "ToolResolutionStatus",
]
