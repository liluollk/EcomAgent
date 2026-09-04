"""
渠道管理路由 — 新增/查询/修改/删除/测连通。

依赖 transport.state 中的共享状态，以及 channel_registry 的配置化融合。
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse

from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

router = APIRouter()


@router.get("/sources")
async def list_sources() -> JSONResponse:
    """列出全部渠道配置（敏感字段已掩码）。"""
    return JSONResponse(content=DEFAULT_CHANNEL_REGISTRY.list())


@router.post("/sources")
async def add_source(payload: dict) -> JSONResponse:
    """新增渠道：name 必填且唯一，label / base_url / auth_type / enabled 可选。"""
    if not isinstance(payload, dict) or not payload.get("name"):
        return JSONResponse(content={"error": "name 必填"}, status_code=400)
    try:
        cfg = DEFAULT_CHANNEL_REGISTRY.add(payload)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=cfg)


@router.patch("/sources/{name}")
async def update_source(name: str, payload: dict) -> JSONResponse:
    """更新渠道配置（base_url / auth_type / enabled 等），命中触发 REST client 重建。"""
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "请求体必须是对象"}, status_code=400)
    try:
        cfg = DEFAULT_CHANNEL_REGISTRY.update(name, payload)
    except KeyError:
        return JSONResponse(content={"error": f"渠道不存在: {name}"}, status_code=404)
    return JSONResponse(content=DEFAULT_CHANNEL_REGISTRY._masked(cfg))


@router.delete("/sources/{name}")
async def delete_source(name: str) -> JSONResponse:
    """删除渠道（内置三渠道不可删，用于保留演示基线）。"""
    try:
        DEFAULT_CHANNEL_REGISTRY.remove(name)
    except KeyError:
        return JSONResponse(content={"error": f"渠道不存在: {name}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content={"deleted": name})


@router.post("/sources/{name}/test")
async def test_source(name: str) -> JSONResponse:
    """连通性测试：按渠道 platform 路由。

    mock 平台：用该渠道 REST client 发一次只读请求（order-stats）。
    真实平台（taobao/jd/douyin/open）：由对应 PlatformAdapter 的 probe() 判断
    （stub 返回诚实「尚未接入」，不误报连通）。
    """
    cfg = DEFAULT_CHANNEL_REGISTRY.get(name)
    if cfg is None or not cfg.get("enabled", True):
        return JSONResponse(
            content={"ok": False, "message": f"渠道不存在或未启用: {name}"},
            status_code=404,
        )
    platform = str(cfg.get("platform") or "mock")
    if platform != "mock":
        adapter = DEFAULT_CHANNEL_REGISTRY.executor_for(name)
        try:
            result = await adapter.probe(cfg)
        except NotImplementedError as exc:
            return JSONResponse(content={"ok": False, "message": str(exc)})
        except Exception as exc:
            return JSONResponse(content={"ok": False, "message": str(exc)})
        return JSONResponse(content=result)

    client = DEFAULT_CHANNEL_REGISTRY.client_for(name)
    if client is None:
        return JSONResponse(
            content={"ok": False, "message": f"渠道不存在或未启用: {name}"},
            status_code=404,
        )
    try:
        data = await client.call("GET", f"/v1/{name}/order-stats", params={"period": "近7天"})
        return JSONResponse(content={"ok": True, "message": "连通成功", "data": data})
    except Exception as exc:
        return JSONResponse(content={"ok": False, "message": str(exc)})