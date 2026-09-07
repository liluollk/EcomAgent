"""外部工具通道演示服务 — 模拟平台之外的第三方 MCP 工具（汇率/天气等）。

定位：电商业务通道模拟见 mock_commerce/（独立电商服务）；本包只承载
MCP 通道的演示服务端 mocks/mcp_tool_server.py，供 integrations/mcp 客户端池
以 stdio 子进程方式真实拉起，验证「Runtime 作为 MCP Client 接入外部工具」。
"""
