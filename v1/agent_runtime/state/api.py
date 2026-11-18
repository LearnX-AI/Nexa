"""Session state and state audit API (NEXA-M3-006), served by the Agent Runtime.

  POST   /v1/sessions                      {"workflows": [...], "ttl_s": 86400}
  GET    /v1/sessions/{id}                 owner: values and metadata; admin: metadata only
  PUT    /v1/sessions/{id}/state           owner: {"data": {...}, "expected_version": n}
  DELETE /v1/sessions/{id}                 owner or admin
  GET    /v1/state/access                  ?target= &actor=   auditors: who accessed which state
  GET    /v1/state/schema                  nexa.state/1.0
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from v1.identity.principal import Principal
from v1.identity.rbac import can

from ..auth import require_principal
from ..orchestration import engine
from . import contract as sc, store as state

router = APIRouter(tags=["state"])


class NewSession(BaseModel):
    workflows: list[str] = Field(min_length=1, max_length=20)
    ttl_s: int = Field(sc.SESSION_TTL_DEFAULT, ge=60, le=sc.SESSION_TTL_MAX)


class SessionWrite(BaseModel):
    data: dict[str, Any]
    expected_version: int = Field(ge=1)


def _tid(request: Request) -> Optional[str]:
    return getattr(request.state, "trace_id", None)


def _call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except sc.StateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _out(rec: sc.StateRecord) -> dict[str, Any]:
    return rec.dump()


@router.post("/v1/sessions", status_code=201)
async def create(body: NewSession, request: Request, principal: Principal = Depends(require_principal)):
    if not can(principal, "model.invoke"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return _out(_call(state.create_session, principal, body.workflows, body.ttl_s,
                      known_workflows=set(engine.WORKFLOWS), trace_id=_tid(request)))


@router.get("/v1/sessions/{session_id}")
async def get(session_id: str, request: Request, principal: Principal = Depends(require_principal)):
    meta = _call(state.session_metadata, session_id, principal, trace_id=_tid(request))
    if meta["owner"] != principal.subject:
        return {"metadata": meta}
    return {"metadata": meta, "record": _out(_call(state.read_session, session_id, principal, trace_id=_tid(request)))}


@router.put("/v1/sessions/{session_id}/state")
async def put(session_id: str, body: SessionWrite, request: Request, principal: Principal = Depends(require_principal)):
    return _out(_call(state.write_session, session_id, principal, body.data, expected_version=body.expected_version,
                      trace_id=_tid(request)))


@router.delete("/v1/sessions/{session_id}")
async def delete(session_id: str, request: Request, reason: str = Query("", max_length=300),
                 principal: Principal = Depends(require_principal)):
    _call(state.delete_session, session_id, principal, reason, trace_id=_tid(request))
    return {"session_id": session_id, "deleted": True}


@router.get("/v1/state/access")
async def access(target: Optional[str] = None, actor: Optional[str] = None, limit: int = Query(200, ge=1, le=2000),
                 principal: Principal = Depends(require_principal)):
    if not set(principal.roles) & state.ACCESS_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted")
    return {"access": state.access_log(target, actor, limit)}


@router.get("/v1/state/schema")
async def schema(principal: Principal = Depends(require_principal)):
    return sc.json_schema()
