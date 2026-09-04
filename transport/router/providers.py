"""
模型供应商管理路由 — 新增/查询/修改/删除/激活/测连通。

依赖 agent_backend.provider_registry 的配置化注册表，以及 transport.state 的共享配置。
前端设置页据此实现「切换供应商 / 切模型 / 填 API Key / base_url」的即时生效配置。
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from starlette.responses import JSONResponse

from agent_backend.provider_registry import DEFAULT_PROVIDER_REGISTRY

router = APIRouter()


@router.get("/providers")
async def list_providers() -> JSONResponse:
    """列出全部模型供应商（api_key 掩码）+ 当前激活者。"""
    return JSONResponse(
        content={
            "providers": DEFAULT_PROVIDER_REGISTRY.list(),
            "active": DEFAULT_PROVIDER_REGISTRY.active_name(),
        }
    )


@router.post("/providers")
async def add_provider(payload: dict) -> JSONResponse:
    """新增供应商：name 必填且唯一，provider 必须合法（openai / anthropic / mock）。"""
    if not isinstance(payload, dict) or not payload.get("name"):
        return JSONResponse(content={"error": "name 必填"}, status_code=400)
    try:
        cfg = DEFAULT_PROVIDER_REGISTRY.add(payload)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=cfg)


@router.patch("/providers/{name}")
async def update_provider(name: str, payload: dict) -> JSONResponse:
    """更新供应商配置（api_key / api_base / default_model / enabled 等），保存即生效。"""
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "请求体必须是对象"}, status_code=400)
    try:
        cfg = DEFAULT_PROVIDER_REGISTRY.update(name, payload)
    except KeyError:
        return JSONResponse(content={"error": f"供应商不存在: {name}"}, status_code=404)
    return JSONResponse(content=DEFAULT_PROVIDER_REGISTRY._masked(dict(cfg)))


@router.delete("/providers/{name}")
async def delete_provider(name: str) -> JSONResponse:
    """删除供应商（内置 openai / anthropic / mock 不可删）。"""
    try:
        DEFAULT_PROVIDER_REGISTRY.remove(name)
    except KeyError:
        return JSONResponse(content={"error": f"供应商不存在: {name}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content={"deleted": name})


@router.post("/providers/{name}/activate")
async def activate_provider(name: str) -> JSONResponse:
    """将指定供应商设为当前激活者（配置即时生效，新 WS 连接用此供应商构建后端）。"""
    try:
        cfg = DEFAULT_PROVIDER_REGISTRY.set_active(name)
    except KeyError:
        return JSONResponse(
            content={"ok": False, "message": f"供应商不存在: {name}"}, status_code=404
        )
    return JSONResponse(content={"ok": True, "active": name, "provider": cfg})


@router.post("/providers/{name}/test")
async def test_provider(name: str) -> JSONResponse:
    """连通性测试：校验该供应商配置能否构建后端实例（离线，不实际调用 LLM 网络）。"""
    cfg = DEFAULT_PROVIDER_REGISTRY.get(name)
    if cfg is None:
        return JSONResponse(
            content={"ok": False, "message": f"供应商不存在: {name}"}, status_code=404
        )
    provider = cfg.get("provider", "openai")
    # mock 后端不依赖网络密钥，始终可构建。
    if provider == "mock":
        return JSONResponse(content={"ok": True, "message": "Mock 后端就绪（离线演示，无需密钥）"})
    try:
        DEFAULT_PROVIDER_REGISTRY.build_config(name)
    except Exception as exc:
        return JSONResponse(content={"ok": False, "message": f"配置无效: {exc}"})
    key = str(cfg.get("api_key") or "").strip() or os.environ.get(
        {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider, "")
    )
    if not key:
        return JSONResponse(
            content={
                "ok": False,
                "message": f"{provider} 未配置 API Key（或环境变量），填密钥后可用",
            }
        )
    return JSONResponse(
        content={"ok": True, "message": f"{provider} 配置有效（未发起真实网络请求，需真实调用时以运行时为准）"}
    )