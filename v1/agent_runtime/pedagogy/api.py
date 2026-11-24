"""The Teaching Intelligence Policy, readable by anyone signed in (NEXA-M4-001).

  GET /v1/teaching-policy                      released versions, and which agents use which
  GET /v1/teaching-policy/{version}            one version, whole, with its digest
  GET /v1/teaching-policy/{version}/compose    what a call would add (profile, audience, grade);
                                               ai_engineer, admin and platform_admin only
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from v1.identity.principal import Principal

from .. import registry
from ..auth import require_principal
from . import contract as pc

router = APIRouter()
COMPOSE_ROLES = {"ai_engineer", "admin", "platform_admin"}


def _used_by(version: str) -> list[dict]:
    out = []
    for aid, spec in sorted(registry.AGENTS.items()):
        if spec.pedagogy_version == version:
            out.append({"agent": aid, "version": spec.version, "profile": spec.pedagogy_profile})
    return out


@router.get("/v1/teaching-policy")
async def list_policies(principal: Principal = Depends(require_principal)):
    rel = pc._released()
    return {"policy": pc.POLICY_ID, "versions": [
        {"version": v, "status": pc.get(v).status, "digest": rel[v], "released_on": pc.get(v).released_on,
         "used_by": _used_by(v)} for v in pc.versions()]}


@router.get("/v1/teaching-policy/{version}")
async def get_policy(version: str, principal: Principal = Depends(require_principal)):
    try:
        p = pc.get(version)
    except pc.PedagogyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"digest": pc._released()[version], "used_by": _used_by(version), "policy": p.model_dump(mode="json")}


@router.get("/v1/teaching-policy/{version}/compose")
async def preview(version: str, profile: str = "tutor", audience: Literal["student", "teacher"] = "student",
                  grade: Optional[str] = None, principal: Principal = Depends(require_principal)):
    if not set(principal.roles) & COMPOSE_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted")
    try:
        c = pc.compose(pc.get(version), profile=profile, audience=audience, grade=grade)
    except pc.PedagogyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"text": c.text, "record": c.record}
