"""MCP 路由 — server 配置 CRUD（前端可接入外部 MCP）+ 连接状态聚合。

- /mcp/servers：data/mcp_servers.json 的增删改查（用户自行接入想用的 MCP）；
- /mcp/servers/{name}/test：连通性测试（拉起子进程 → initialize → 关闭）；
- /mcp/status：运行时连接状态 + 各 server 已发现工具聚合。
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse

from integrations.mcp.client_pool import McpClientPool
from integrations.mcp.server_config import DEFAULT_MCP_SERVER_REGISTRY, to_params
from transport.state import mcp_pool

router = APIRouter()


def _mask_server(cfg: dict) -> dict:
    """env 值掩码（可能含密钥），其余字段原样返回。"""
    out = dict(cfg)
    env = out.get("env")
    if isinstance(env, dict):
        out["env"] = {k: ("****" if v else "") for k, v in env.items()}
    return out


@router.get("/mcp/servers")
async def list_servers() -> JSONResponse:
    """列出全部 MCP server 配置（env 值掩码）。"""
    return JSONResponse(content=[_mask_server(s) for s in DEFAULT_MCP_SERVER_REGISTRY.list()])


@router.post("/mcp/servers")
async def add_server(payload: dict) -> JSONResponse:
    """新增 MCP server：name/command 必填且唯一，args/env 可选。"""
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "请求体必须是对象"}, status_code=400)
    try:
        saved = DEFAULT_MCP_SERVER_REGISTRY.add(payload)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=_mask_server(saved))


@router.patch("/mcp/servers/{name}")
async def update_server(name: str, payload: dict) -> JSONResponse:
    """更新 MCP server（command/args/env/enabled），保存后下次连接生效。"""
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "请求体必须是对象"}, status_code=400)
    try:
        saved = DEFAULT_MCP_SERVER_REGISTRY.update(name, payload)
    except KeyError:
        return JSONResponse(content={"error": f"MCP server 不存在: {name}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=_mask_server(saved))


@router.delete("/mcp/servers/{name}")
async def delete_server(name: str) -> JSONResponse:
    """删除 MCP server 配置。"""
    try:
        DEFAULT_MCP_SERVER_REGISTRY.remove(name)
    except KeyError:
        return JSONResponse(content={"error": f"MCP server 不存在: {name}"}, status_code=404)
    return JSONResponse(content={"deleted": name})


@router.post("/mcp/servers/{name}/test")
async def test_server(name: str) -> JSONResponse:
    """连通性测试：按配置拉起子进程，initialize 握手并发现工具后关闭。"""
    cfg = DEFAULT_MCP_SERVER_REGISTRY.get(name)
    if cfg is None:
        return JSONResponse(content={"error": f"MCP server 不存在: {name}"}, status_code=404)
    probe = McpClientPool()
    try:
        await probe.connect(params=to_params(cfg))
        tools = [
            {"name": t.get("name", ""), "description": t.get("description", "")}
            for t in probe.get_all_tool_definitions()
        ]
        return JSONResponse(content={"ok": True, "tools": tools})
    except Exception as exc:
        return JSONResponse(content={"ok": False, "error": str(exc)})
    finally:
        await probe.close()


@router.get("/mcp/status")
async def mcp_status() -> JSONResponse:
    """MCP 连接状态 + 各 server 工具清单聚合（name / description）。"""
    servers = mcp_pool.server_status()
    tools = mcp_pool.get_all_tool_definitions()
    return JSONResponse(
        content={
            "connected": bool(mcp_pool.connected),
            "transport": "stdio / JSON-RPC",
            "servers": servers,
            "tools": [
                {"name": t.get("name", ""), "description": t.get("description", "")}
                for t in tools
            ],
        }
    )