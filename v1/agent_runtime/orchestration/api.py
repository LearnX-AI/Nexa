"""Orchestration API (NEXA-M3-003), served by the Agent Runtime.

  GET  /v1/workflows                          the workflows and their patterns
  GET  /v1/workflows/{id}                     one definition (nexa.workflow/1.0)
  POST /v1/workflows/{id}/runs?wait=          start a run; wait=false returns 202 at once
  GET  /v1/workflow-runs?status=              runs the caller may see (a reviewer's queue)
  GET  /v1/workflow-runs/{run_id}             a run with its full trace
  POST /v1/workflow-runs/{run_id}/decision    approve or reject at a checkpoint

Who may run a workflow: its allowed_roles holding its run_permission. Who may
see a run: the person who started it, the reviewers of the checkpoint it waits
at, and admin, platform_admin, auditor and ai_engineer.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from v1.identity.principal import Principal
from v1.identity.rbac import can

from ..auth import require_principal
from ..messaging import bus, contract as mc
from ..state import store as state
from . import engine

router = APIRouter(tags=["orchestration"])


class StartRun(BaseModel):
    input: dict[str, Any]
    session_id: Optional[str] = Field(None, max_length=60, description="Session state to use (NEXA-M3-006).")


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field("", max_length=2000)


async def _do(coro):
    try:
        return await coro
    except engine.OrchestrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _summary(wf) -> dict[str, Any]:
    return {"id": wf.id, "name": wf.name, "version": wf.version, "state": wf.state, "pattern": wf.pattern,
            "description": wf.description, "allowed_roles": wf.allowed_roles, "input": wf.input,
            "agents": sorted(wf.agents()), "budget": wf.budget.model_dump(),
            "steps": [{"id": s.id, "type": s.type, "title": s.title} for s in wf.steps],
            "fingerprint": wf.fingerprint()}


@router.get("/v1/workflows")
async def list_workflows(principal: Principal = Depends(require_principal)):
    if not can(principal, "agents.read"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return {"workflows": [_summary(w) for w in engine.WORKFLOWS.values()]}


@router.get("/v1/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, principal: Principal = Depends(require_principal)):
    if not can(principal, "agents.read"):
        raise HTTPException(status_code=403, detail="Not permitted")
    wf = engine.WORKFLOWS.get(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_id}")
    return {"workflow": wf.model_dump(mode="json", by_alias=True), "fingerprint": wf.fingerprint()}


@router.post("/v1/workflows/{workflow_id}/runs", status_code=201)
async def start_run(workflow_id: str, body: StartRun, request: Request, wait: bool = True,
                    principal: Principal = Depends(require_principal),
                    authorization: Optional[str] = Header(default=None),
                    idempotency_key: Optional[str] = Header(default=None, max_length=200)):
    out = await _do(engine.start(workflow_id, principal, body.input, auth=authorization, wait=wait,
                                 trace_id=getattr(request.state, "trace_id", None), idempotency_key=idempotency_key,
                                 session_id=body.session_id))
    if out.get("duplicate"):
        return JSONResponse(status_code=200, content=out)
    if not wait:
        return JSONResponse(status_code=202, content=out)
    return out


@router.get("/v1/workflow-runs")
async def list_runs(status: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
                    principal: Principal = Depends(require_principal)):
    return {"runs": engine.list_runs(principal, status, limit)}


@router.get("/v1/workflow-runs/{run_id}")
async def get_run(run_id: str, trace: bool = True, principal: Principal = Depends(require_principal)):
    run = engine.load_run(run_id)
    if run is None or not engine.may_view(run, principal):
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return engine.view(run, with_trace=trace)


@router.get("/v1/workflow-runs/{run_id}/messages")
async def run_messages(run_id: str, principal: Principal = Depends(require_principal)):
    """The run's conversation: every nexa.message/1.0 envelope in order, and
    the result of checking it against the contract (NEXA-M3-005)."""
    run = engine.load_run(run_id)
    if run is None or not engine.may_view(run, principal):
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return {"run_id": run_id, "check": bus.check_conversation(run_id), "messages": bus.conversation(run_id)}


@router.post("/v1/workflow-runs/{run_id}/resume")
async def resume_run(run_id: str, wait: bool = True, principal: Principal = Depends(require_principal),
                     authorization: Optional[str] = Header(default=None)):
    """Resume an interrupted run from its last valid checkpoint (the person who
    started it, with their own session)."""
    return await _do(engine.resume(run_id, principal, auth=authorization, wait=wait))


@router.get("/v1/workflow-runs/{run_id}/checkpoints")
async def run_checkpoints(run_id: str, principal: Principal = Depends(require_principal)):
    run = engine.load_run(run_id)
    if run is None or not engine.may_view(run, principal):
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    state.log_access(principal, "read", "run", run_id, keys=["checkpoints"])
    return {"run_id": run_id, "checkpoints": state.checkpoints(run_id), "resumable": engine.resumable(run) is None
            if run.status == "interrupted" else False}


@router.get("/v1/messaging/schema")
async def messaging_schema(principal: Principal = Depends(require_principal)):
    if not can(principal, "agents.read"):
        raise HTTPException(status_code=403, detail="Not permitted")
    return mc.json_schema()


@router.post("/v1/workflow-runs/{run_id}/decision")
async def decide(run_id: str, body: Decision, wait: bool = True, principal: Principal = Depends(require_principal)):
    return await _do(engine.decide(run_id, principal, body.decision, body.comment, wait=wait))
