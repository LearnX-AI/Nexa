"""Agent policy API (NEXA-M3-007), served by the Agent Runtime.

  GET /v1/policy/agents              every agent's policy and its fingerprint
  GET /v1/policy/agents/{id}
  GET /v1/policy/decisions           ?agent= &action= &decision= &trace_id= &run_id= &actor=  (auditors)
  GET /v1/policy/schema              nexa.agent-policy/1.0
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from v1.identity.principal import Principal
from v1.identity.rbac import can

from ..auth import require_principal
from . import contract as pc, pdp

router = APIRouter(tags=["agent-policy"])
AUDIT_ROLES = {"auditor", "admin", "platform_admin", "ai_engineer"}


def _view(p: pc.AgentPolicy) -> dict:
    return p.model_dump(mode="json") | {"fingerprint": p.fingerprint(), "identity": f"agent:{p.agent}"}


@router.get("/v1/policy/agents")
async def list_policies(principal: Principal = Depends(require_principal)):
    if not can(principal, "agents.read"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return {"policies": [_view(p) for p in pdp.POLICIES.values()]}


@router.get("/v1/policy/agents/{agent}")
async def get_policy(agent: str, principal: Principal = Depends(require_principal)):
    if not can(principal, "agents.read"):
        raise HTTPException(status_code=403, detail="Not permitted")
    p = pdp.POLICIES.get(agent)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No policy for {agent}: it may do nothing")
    return _view(p)


@router.get("/v1/policy/decisions")
async def decisions(agent: Optional[str] = None, action: Optional[str] = None, decision: Optional[str] = None,
                    trace_id: Optional[str] = None, run_id: Optional[str] = None, actor: Optional[str] = None,
                    limit: int = Query(200, ge=1, le=2000), principal: Principal = Depends(require_principal)):
    if not set(principal.roles) & AUDIT_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted")
    return {"decisions": pdp.decisions(agent=agent, action=action, decision=decision, trace_id=trace_id,
                                       run_id=run_id, actor=actor, limit=limit)}


@router.get("/v1/policy/schema")
async def schema(principal: Principal = Depends(require_principal)):
    return pc.json_schema()
