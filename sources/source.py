"""
Source — 外部系统能力接入的抽象层。

Source 概念。
每个 Source 代表一个外部系统（如淘宝开放平台、京东开放平台），
包含工具定义和对应的处理函数。

工具定义采用 OpenAI function calling 格式：
{
    "name": "query_inventory",
    "description": "查询商品库存",
    "parameters": { "type": "object", "properties": {...} }
}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Tool:
    """工具定义，包含元数据和处理函数。

    Attributes:
        name: 工具名称，如 "query_inventory"。
        description: 工具描述，LLM 据此判断是否调用。
        parameters: 工具参数 JSON Schema。
        handler: 工具处理函数，签名: (**kwargs) -> str | dict。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Optional[Callable[..., Any]] = None


@dataclass
class Source:
    """Source 抽象 — 外部系统能力接入。

    Attributes:
        name: Source 名称，如 "taobao"、"jd"、"douyin"。
        description: Source 描述。
        type: 连接类型，如 "mcp"、"rest"、"graphql"。
        tools: 该 Source 提供的工具列表。
    """

    name: str
    description: str
    type: str = "mcp"
    tools: list[Tool] = field(default_factory=list)

    def add_tool(self, tool: Tool) -> None:
        """向 Source 添加一个工具。

        Args:
            tool: 工具定义对象。
        """
        self.tools.append(tool)

    def get_tool_handler(self, tool_name: str) -> Optional[Callable[..., Any]]:
        """根据工具名称获取处理函数。

        Args:
            tool_name: 工具名称。

        Returns:
            Callable | None: 处理函数，不存在返回 None。
        """
        for tool in self.tools:
            if tool.name == tool_name:
                return tool.handler
        return None

    def get_tool_definitions(self) -> list[dict]:
        """获取所有工具的 OpenAI function calling 格式定义。

        Returns:
            list[dict]: 工具定义列表。
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self.tools
        ]