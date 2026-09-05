"""
技能管理路由 — 新增/查询/修改/删除/启停技能。

依赖 sources.skill_registry 的文件制注册表（内置 sources/skills_builtin/
+ 用户 data/skills/，SKILL.md 格式）。内置技能只读；用户自定义技能可全量管理。
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse

from sources.skill_registry import (
    DEFAULT_SKILL_REGISTRY,
    Skill,
)

router = APIRouter()


def _skill_payload(skill: Skill) -> dict:
    return {
        "name": skill.name,
        "description": skill.description,
        "keywords": skill.keywords,
        "prerequisites": skill.prerequisites,
        "body": skill.body,
        "enabled": skill.enabled,
        "builtin": skill.builtin,
    }


@router.get("/skills")
async def list_skills() -> JSONResponse:
    """列出全部技能（含 disabled）。"""
    return JSONResponse(content=[_skill_payload(s) for s in DEFAULT_SKILL_REGISTRY.list()])


@router.post("/skills")
async def add_skill(payload: dict) -> JSONResponse:
    """新增技能：name 必填且唯一，keywords/prerequisites/body 可选。"""
    if not isinstance(payload, dict) or not payload.get("name"):
        return JSONResponse(content={"error": "name 必填"}, status_code=400)
    try:
        skill = Skill(
            name=str(payload["name"]).strip(),
            description=str(payload.get("description") or payload["name"]),
            keywords=[str(k) for k in payload.get("keywords") or []],
            prerequisites=[str(p) for p in payload.get("prerequisites") or []],
            body=str(payload.get("body") or payload.get("prompt") or ""),
            enabled=bool(payload.get("enabled", True)),
        )
        saved = DEFAULT_SKILL_REGISTRY.add(skill)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=_skill_payload(saved))


@router.patch("/skills/{name}")
async def update_skill(name: str, payload: dict) -> JSONResponse:
    """更新用户技能（keywords / body / enabled 等），保存即生效。内置技能只读。"""
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "请求体必须是对象"}, status_code=400)
    if "prompt" in payload:
        payload = {**payload, "body": payload.pop("prompt")}
    try:
        saved = DEFAULT_SKILL_REGISTRY.update(name, payload)
    except KeyError:
        return JSONResponse(content={"error": f"技能不存在: {name}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=_skill_payload(saved))


@router.delete("/skills/{name}")
async def delete_skill(name: str) -> JSONResponse:
    """删除用户技能（内置技能不可删）。"""
    try:
        DEFAULT_SKILL_REGISTRY.remove(name)
    except KeyError:
        return JSONResponse(content={"error": f"技能不存在: {name}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content={"deleted": name})