"""Tool 公共数据定义(架构文档 §3.2)。

ToolDefinition 只包含模型可见且可持久化的数据与版本,不包含回调、闭包、
客户端对象、数据库 Session 或凭证;Handler 由启动时组装的 ToolBinding
(Worker 进程内,架构文档 §3.3)连接。
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ToolEffect(str, Enum):
    """Tool 副作用分类(架构文档 §3.2:effect: READ | WRITE)。"""

    READ = "READ"
    WRITE = "WRITE"


class ToolDefinition(BaseModel):
    """模型可见且可持久化的 Tool 数据定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stable_key: str
    version: int
    description: str
    input_json_schema: dict[str, Any]
    output_json_schema: dict[str, Any]
    effect: ToolEffect
    required_permissions: frozenset[str]
    source_kind: Literal["LOCAL", "MCP"]
    schema_hash: str
