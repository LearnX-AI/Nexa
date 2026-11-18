"""Agent registry and discovery API (NEXA-M3-002), served by the Agent Runtime.

  GET  /v1/registry/agents                     query: capability, audience, state, runnable
  POST /v1/registry/agents                     register a manifest (new agent or new version)
  GET  /v1/registry/agents/{id}                the agent, its versions and deployments
  GET  /v1/registry/agents/{id}/versions/{v}   one version's manifest as registered
  PUT  /v1/registry/agents/{id}/versions/{v}/state   lifecycle change
  POST /v1/registry/agents/{id}/disable        kill switch (reason required)
  POST /v1/registry/agents/{id}/enable
  GET  /v1/registry/agents/{id}/history        audit trail of the agent
  GET  /v1/registry/history                    audit trail of the registry
  GET  /v1/registry/discover                   agents this caller may use for a capability
  GET  /v1/registry/resolve/{id}               the deployment a request should go to
  POST /v1/registry/deployments                record a deployment (Agent Runtime instances)
  POST /v1/registry/deployments/{id}/heartbeat health of a deployment

Permissions (rbac.py): agents.read to read and discover; agents.register to
register; agents.approve for review, approval and activation; agents.admin to
suspend, deprecate, retire, disable and enable; agents.deploy for deployments.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import audit_event

from . import registry
from .auth import require_principal

router = APIRouter(prefix="/v1/registry", tags=["agent-registry"])

# Which permission each lifecycle target needs.
APPROVE_STATES = {"draft", "review", "approved", "active"}
ADMIN_STATES = {"suspended", "deprecated", "retired"}


def _need(principal: Principal, permission: str, action: str) -> None:
    if not can(principal, permission):
        audit_event(action=f"agent.registry.{action}", decision="deny", principal=principal.subject,
                    permission=permission)
        raise HTTPException(status_code=403, detail=f"Not permitted (needs {permission})")


def _reg() -> registry.Registry:
    if registry.REGISTRY is None:
        raise HTTPException(status_code=503, detail="Agent registry not open")
    return registry.REGISTRY


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except registry.RegistryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


class StateChange(BaseModel):
    state: str
    reason: str = Field("", max_length=500)
    approved_by: Optional[str] = Field(None, max_length=120)
    approved_on: Optional[str] = None
    sunset_on: Optional[str] = None
    replaced_by: Optional[str] = None


class Reason(BaseModel):
    reason: str = Field("", max_length=500)


class Deployment(BaseModel):
    agent_id: str
    version: str
    location: str = Field(pattern=r"^[a-z]+:[a-z0-9._-]+$", description="core:dgx, school:pomnsoe, ...")
    endpoint: str = Field(max_length=300)
    instance: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,100}$")


class Heartbeat(BaseModel):
    ready: bool
    checks: dict[str, Any] = Field(default_factory=dict)


@router.get("/agents")
async def list_agents(capability: Optional[str] = None, audience: Optional[str] = None,
                      state: Optional[str] = None, runnable: bool = False,
                      principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "read")
    return {"agents": _reg().query(capability=capability, audience=audience, state=state,
                                   runnable_only=runnable)}


@router.post("/agents", status_code=201)
async def register(manifest: dict[str, Any], principal: Principal = Depends(require_principal)):
    _need(principal, "agents.register", "register")
    return _call(_reg().register, manifest, principal.subject)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "read")
    reg = _reg()
    s = reg.summary(agent_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    a = reg.agent_row(agent_id)
    return s | {"version_detail": reg.versions(agent_id), "disabled_by": a["disabled_by"],
                "disabled_at": a["disabled_at"], "deployments": reg.deployments(agent_id)}


@router.get("/agents/{agent_id}/versions/{version}")
async def get_version(agent_id: str, version: str, principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "read")
    m = _reg().manifest(agent_id, version)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent version: {agent_id} {version}")
    return {"manifest": m.model_dump(mode="json"), "fingerprint": m.fingerprint(),
            "content_fingerprint": m.content_fingerprint()}


@router.put("/agents/{agent_id}/versions/{version}/state")
async def set_state(agent_id: str, version: str, body: StateChange,
                    principal: Principal = Depends(require_principal)):
    if body.state in APPROVE_STATES:
        _need(principal, "agents.approve", "state")
    elif body.state in ADMIN_STATES:
        _need(principal, "agents.admin", "state")
    else:
        raise HTTPException(status_code=422, detail=f"Unknown lifecycle state: {body.state}")
    return _call(_reg().set_state, agent_id, version, body.state, principal.subject,
                 approved_by=body.approved_by, approved_on=body.approved_on, sunset_on=body.sunset_on,
                 replaced_by=body.replaced_by, reason=body.reason)


@router.post("/agents/{agent_id}/disable")
async def disable(agent_id: str, body: Reason, principal: Principal = Depends(require_principal)):
    _need(principal, "agents.admin", "disable")
    return _call(_reg().set_enabled, agent_id, False, principal.subject, body.reason)


@router.post("/agents/{agent_id}/enable")
async def enable(agent_id: str, body: Reason, principal: Principal = Depends(require_principal)):
    _need(principal, "agents.admin", "enable")
    return _call(_reg().set_enabled, agent_id, True, principal.subject, body.reason)


@router.get("/agents/{agent_id}/history")
async def agent_history(agent_id: str, limit: int = Query(100, ge=1, le=1000),
                        principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "read")
    if _reg().agent_row(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return {"agent_id": agent_id, "events": _reg().history(agent_id, limit)}


@router.get("/history")
async def history(limit: int = Query(100, ge=1, le=1000), principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "read")
    return {"events": _reg().history(None, limit)}


@router.get("/discover")
async def discover(capability: Optional[str] = None, location: Optional[str] = None,
                   principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "discover")
    return {"capability": capability, "location": location,
            "agents": _reg().discover(principal, capability=capability, location=location)}


@router.get("/resolve/{agent_id}")
async def resolve(agent_id: str, location: Optional[str] = None, version: Optional[str] = None,
                  principal: Principal = Depends(require_principal)):
    _need(principal, "agents.read", "resolve")
    r = _reg().resolve(agent_id, location=location, version=version)
    if r is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return r


@router.post("/deployments", status_code=201)
async def add_deployment(body: Deployment, principal: Principal = Depends(require_principal)):
    _need(principal, "agents.deploy", "deploy")
    return _call(_reg().register_deployment, body.agent_id, body.version, location=body.location,
                 endpoint=body.endpoint, instance=body.instance, actor=principal.subject)


@router.post("/deployments/{deployment_id:path}/heartbeat")
async def heartbeat(deployment_id: str, body: Heartbeat, principal: Principal = Depends(require_principal)):
    _need(principal, "agents.deploy", "heartbeat")
    _call(_reg().heartbeat, deployment_id, body.model_dump(), principal.subject)
    return {"deployment_id": deployment_id, "ready": body.ready}
