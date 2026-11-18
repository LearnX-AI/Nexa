"""NEXA Tool & Connector Gateway (NEXA-M3-004, ADR-0006). Port 8105.

The only way agents reach tools, APIs and connectors. Only the Agent Runtime
calls it: with its own service token (Authorization), the user's token
(X-NEXA-On-Behalf-Of) and the agent it is calling for (X-NEXA-Agent).

  GET  /health
  GET  /v1/tools                     the catalog (contracts)
  GET  /v1/tools/{id}
  POST /v1/tools/{id}/invoke         {"arguments": {...}, "scopes": [...]}
  GET  /v1/tools/audit               ?trace_id= &run_id= &tool= &user= &call_id=  (auditors)
  GET  /v1/connectors                connectors, without credentials (admins)

Run:
    export NEXA_IDENTITY_SECRET=...
    uvicorn v1.tool_gateway.main:app --host 127.0.0.1 --port 8105
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.identity.tokens import TokenError, verify_token
from v1.observability import TRACE_HEADER, TraceMiddleware, metrics_endpoint
from v1.shared.config import preflight

from . import gateway

app = FastAPI(title="NEXA Tool Gateway", version="1.0.0")
app.add_middleware(TraceMiddleware, service="nexa-tool-gateway")
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
gateway.load()

AUDIT_ROLES = {"auditor", "admin", "platform_admin", "ai_engineer"}


@app.on_event("startup")
async def _startup():
    preflight("nexa-tool-gateway", ["NEXA_IDENTITY_SECRET"])


def _principal(token: str) -> Principal:
    try:
        c = verify_token(token)
    except TokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return Principal(subject=c["sub"], roles=tuple(c.get("roles", ())), token_type=c.get("typ", "service"))


async def bearer(authorization: Optional[str] = Header(default=None)) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return _principal(authorization.split(" ", 1)[1])


class Invoke(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    scopes: Optional[list[str]] = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexa-tool-gateway", "version": app.version, "contract": "nexa.tool/1.0",
            "tools": {t.id: {"version": t.version, "state": t.state, "implementation": t.implementation.kind}
                      for t in gateway.TOOLS.values()},
            "connectors": {c["id"]: {"kind": c["kind"], "approved": c["approved"], "ready": c["url_configured"]
                                     and c["credential_configured"]} for c in gateway.connectors_view()}}


@app.get("/v1/tools")
async def list_tools(principal: Principal = Depends(bearer)):
    return {"tools": gateway.catalog(principal)}


@app.get("/v1/tools/audit")
async def audit(trace_id: Optional[str] = None, run_id: Optional[str] = None, tool: Optional[str] = None,
                user: Optional[str] = None, call_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000),
                principal: Principal = Depends(bearer)):
    if not set(principal.roles) & AUDIT_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted")
    return {"calls": gateway.AUDIT.query(trace_id=trace_id, run_id=run_id, tool=tool, user=user, call_id=call_id,
                                         limit=limit)}


@app.get("/v1/tools/{tool_id}")
async def get_tool(tool_id: str, principal: Principal = Depends(bearer)):
    for t in gateway.catalog(principal):
        if t["id"] == tool_id:
            return t
    raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_id}")


@app.get("/v1/connectors")
async def list_connectors(principal: Principal = Depends(bearer)):
    if not can(principal, "admin") and "ai_engineer" not in principal.roles:
        raise HTTPException(status_code=403, detail="Not permitted")
    return {"connectors": gateway.connectors_view()}


@app.post("/v1/tools/{tool_id}/invoke")
async def invoke(tool_id: str, body: Invoke, request: Request, service: Principal = Depends(bearer),
                 x_nexa_on_behalf_of: Optional[str] = Header(default=None),
                 x_nexa_agent: Optional[str] = Header(default=None),
                 x_nexa_agent_version: Optional[str] = Header(default=None),
                 x_nexa_run: Optional[str] = Header(default=None),
                 x_nexa_step: Optional[str] = Header(default=None)):
    if not x_nexa_on_behalf_of or not x_nexa_agent:
        raise HTTPException(status_code=400, detail="X-NEXA-On-Behalf-Of (the user's token) and X-NEXA-Agent "
                                                    "are required")
    user_token = x_nexa_on_behalf_of.split(" ", 1)[1] if x_nexa_on_behalf_of.startswith("Bearer ") \
        else x_nexa_on_behalf_of
    user = _principal(user_token)
    caller = gateway.Caller(service=service, user=user, user_token=user_token, agent=x_nexa_agent,
                            agent_version=x_nexa_agent_version, run_id=x_nexa_run, step=x_nexa_step,
                            trace_id=getattr(request.state, "trace_id", None) or request.headers.get(TRACE_HEADER))
    try:
        return await gateway.invoke(tool_id, caller, body.arguments, body.scopes)
    except gateway.GatewayError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "call_id": exc.call_id})
