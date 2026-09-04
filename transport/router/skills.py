"""
技能管理路由 — 新增/查询/修改/删除/启停技能。

依赖 sources.skill_registry 的持久化注册表（data/skills.json）。
内置 9 技能不可删除（可启停）；用户自定义技能可全量管理。
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.responses import JSONResponse

from sources.skill_registry import (
    BUILTIN_NAMES,
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
        "tools": skill.tools,
        "prompt": skill.prompt,
        "enabled": skill.enabled,
        "builtin": skill.builtin or skill.name in BUILTIN_NAMES,
    }


@router.get("/skills")
async def list_skills() -> JSONResponse:
    """列出全部技能（含 disabled）。"""
    return JSONResponse(content=[_skill_payload(s) for s in DEFAULT_SKILL_REGISTRY.list()])


@router.post("/skills")
async def add_skill(payload: dict) -> JSONResponse:
    """新增技能：name 必填且唯一，keywords/tools/prompt 可选。"""
    if not isinstance(payload, dict) or not payload.get("name"):
        return JSONResponse(content={"error": "name 必填"}, status_code=400)
    try:
        skill = Skill(
            name=str(payload["name"]).strip(),
            description=str(payload.get("description") or payload["name"]),
            keywords=[str(k) for k in payload.get("keywords") or []],
            prerequisites=[str(p) for p in payload.get("prerequisites") or []],
            tools=[str(t) for t in payload.get("tools") or []],
            prompt=str(payload.get("prompt") or ""),
            enabled=bool(payload.get("enabled", True)),
        )
        saved = DEFAULT_SKILL_REGISTRY.add(skill)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=_skill_payload(saved))


@router.patch("/skills/{name}")
async def update_skill(name: str, payload: dict) -> JSONResponse:
    """更新技能（keywords / tools / prompt / enabled 等），保存即生效。"""
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "请求体必须是对象"}, status_code=400)
    try:
        saved = DEFAULT_SKILL_REGISTRY.update(name, payload)
    except KeyError:
        return JSONResponse(content={"error": f"技能不存在: {name}"}, status_code=404)
    return JSONResponse(content=_skill_payload(saved))


@router.delete("/skills/{name}")
async def delete_skill(name: str) -> JSONResponse:
    """删除技能（内置 9 技能不可删，仅可停用）。"""
    try:
        DEFAULT_SKILL_REGISTRY.remove(name)
    except KeyError:
        return JSONResponse(content={"error": f"技能不存在: {name}"}, status_code=404)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content={"deleted": name})