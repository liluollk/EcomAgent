"""
MCP 外部工具模拟服务 — 演示「第三方 MCP 工具接入」通道（工具三通道之二）。

定位：真实电商运营工具是内置平台 API 通道（sources/builtin_tools.py，
经 REST/Adapter 打平台网关）；本服务模拟的是**平台之外的第三方工具**
（汇率、天气这类通用外部能力），用于演示 MCP 协议通道的真实接入形态——
用户可在前端自行配置任意 MCP server（mcp_servers.json），本服务是默认演示项。

分层（协议真实 / 数据模拟）：
  1. MCP 协议层（真实）：FastMCP + stdio 传输，JSON-RPC 初始化/发现/调用；
  2. 业务数据层（模拟）：汇率/天气返回内置假数据，真实外部服务需替换 handler。

运行方式（被 McpClientPool 以子进程拉起）:
    python -m mocks.mcp_tool_server
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

# 服务名：MCP initialize 握手时返回给客户端
SERVER_NAME = "external-tools-demo"
SERVER_VERSION = "2.0.0"

mcp = FastMCP(SERVER_NAME)

# 模拟汇率表（真实场景为外部汇率 API）
_RATES = {"USD": 7.18, "EUR": 7.82, "JPY": 0.048, "GBP": 9.12}
# 模拟天气表（真实场景为外部天气 API）
_WEATHER = {"杭州": "晴 26℃", "上海": "多云 24℃", "北京": "小雨 19℃"}


@mcp.tool()
async def query_exchange_rate(currency: str) -> str:
    """查询币种对人民币的参考汇率（模拟外部汇率服务）。

    Args:
        currency: 币种代码，如 USD / EUR / JPY / GBP。

    Returns:
        str: 汇率文本。
    """
    rate = _RATES.get(currency.upper())
    if rate is None:
        return f"未收录币种 {currency}，当前支持：{'、'.join(_RATES)}"
    return f"{currency.upper()} 对人民币参考汇率：1 {currency.upper()} ≈ {rate} 元（模拟数据）"


@mcp.tool()
async def query_weather(city: str) -> str:
    """查询城市天气（模拟外部天气服务）。

    Args:
        city: 城市名，如 杭州 / 上海 / 北京。

    Returns:
        str: 天气文本。
    """
    weather = _WEATHER.get(city)
    if weather is None:
        return f"未收录城市 {city}，当前支持：{'、'.join(_WEATHER)}"
    return f"{city} 今日天气：{weather}（模拟数据）"


if __name__ == "__main__":
    # stdio 传输启动：供 McpClientPool 以子进程方式拉起并连接
    mcp.run()