"""Observability API (NEXA-M3-008), served by the Agent Runtime.

  GET /v1/observability/runs/{run_id}            the reconstruction (nexa.trace/1.0)
        ?view=operational (default)               operators: ai_engineer, admin, platform_admin, auditor
        ?view=forensic&reason=...                 auditor, admin, platform_admin; content and real
                                                  subjects; the use is logged
        ?format=text                              the readable tree
  GET /v1/observability/traces/{trace_id}        every run under a trace id, and the policy
                                                  decisions made under it outside any run

The Tool Gateway's audit records are fetched with the caller's own token, so
an operator only sees there what the gateway lets them see.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from v1.identity.principal import Principal

from ..auth import require_principal
from ..state import store as state
from . import reconstruct as rc

router = APIRouter(tags=["observability"])
OPERATORS = {"ai_engineer", "admin", "platform_admin", "auditor"}
FORENSIC = {"auditor", "admin", "platform_admin"}
# Tests replace this with an in-process transport to the Tool Gateway.
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


def _view(principal: Principal, view: str, reason: str, target: str) -> rc.View:
    if not set(principal.roles) & OPERATORS:
        state.log_access(principal, "trace", "run", target, decision="deny", reason="not an operator")
        raise HTTPException(status_code=403, detail="Not permitted")
    if view == "forensic":
        if not set(principal.roles) & FORENSIC:
            state.log_access(principal, "forensic", "run", target, decision="deny", reason="role")
            raise HTTPException(status_code=403, detail="The forensic view is for auditors")
        if len(reason.strip()) < 10:
            state.log_access(principal, "forensic", "run", target, decision="deny", reason="no reason given")
            raise HTTPException(status_code=422, detail="The forensic view needs a reason (at least 10 characters)")
        state.log_access(principal, "forensic", "run", target, reason=reason.strip()[:300])
    elif view != "operational":
        raise HTTPException(status_code=422, detail="view is operational or forensic")
    else:
        state.log_access(principal, "trace", "run", target)
    return rc.View(view)


async def _gateway_audit(run_id: str, authorization: Optional[str]):
    url = os.environ.get("NEXA_TOOL_GATEWAY_URL", "http://127.0.0.1:8105").rstrip("/") + "/v1/tools/audit"
    try:
        async with httpx.AsyncClient(timeout=15, transport=TRANSPORT) as client:
            r = await client.get(url, params={"run_id": run_id, "limit": 1000},
                                 headers={"Authorization": authorization or ""})
    except httpx.HTTPError as exc:
        return None, type(exc).__name__
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.json().get("calls", []), None


@router.get("/v1/observability/runs/{run_id}")
async def run_trace(run_id: str, view: str = "operational", reason: str = Query("", max_length=300),
                    format: str = "json", principal: Principal = Depends(require_principal),
                    authorization: Optional[str] = Header(default=None)):
    v = _view(principal, view, reason, run_id)
    calls, err = await _gateway_audit(run_id, authorization)
    rec = rc.build(run_id, view=v, tool_audit=calls, tool_audit_error=err)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    if format == "text":
        return PlainTextResponse(rc.render_text(rec))
    return rec


@router.get("/v1/observability/traces/{trace_id}")
async def trace(trace_id: str, view: str = "operational", reason: str = Query("", max_length=300),
                principal: Principal = Depends(require_principal), authorization: Optional[str] = Header(default=None)):
    from ..policy import pdp
    from .. import registry
    v = _view(principal, view, reason, f"trace:{trace_id}")
    rows = registry.REGISTRY.store.query("SELECT run_id FROM workflow_runs WHERE trace_id = ?", (trace_id,))
    runs = []
    for r in rows:
        calls, err = await _gateway_audit(r["run_id"], authorization)
        runs.append(rc.build(r["run_id"], view=v, tool_audit=calls, tool_audit_error=err))
    outside = [d for d in pdp.decisions(trace_id=trace_id, limit=500) if not d.get("run_id")]
    loose = v.scrub([{"decision_id": d["decision_id"], "ts": d["ts"], "action": d["action"], "decision": d["decision"],
                      "rule": d["rule"], "agent": d.get("agent"), "resource": d["resource"], "actor": v.who(d["actor"]),
                      "reason": d["reason"]} for d in outside])
    if not runs and not loose:
        raise HTTPException(status_code=404, detail=f"Nothing recorded under trace {trace_id}")
    return {"schema": rc.TRACE_SCHEMA, "trace_id": trace_id, "view": v.kind, "runs": runs,
            "policy_decisions_outside_runs": loose}
