"""NEXA API Gateway service (M1). NEXA-ARCH-001 and NEXA-M1-002.

The single external entry. Verifies the caller at the edge, applies CORS, and
proxies to the Agent Runtime. Caller tokens are forwarded unchanged; the runtime
enforces per agent RBAC. The request trace id is forwarded downstream. Errors
are returned in a standard shape carrying the trace id. The model runtime is
never exposed to clients directly.

Run:
    export NEXA_IDENTITY_SECRET=...        # shared, to verify callers
    uvicorn v1.api_gateway.main:app --host 127.0.0.1 --port 8099
"""
from __future__ import annotations
import re
from typing import Any, Optional
import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from v1.identity.principal import Principal
from v1.observability import (
    TraceMiddleware, TRACE_HEADER, audit_event, log_event, metrics_endpoint,
)
from v1.shared.config import preflight

from . import config
from .auth import require_principal
from .schemas import AgentList, ErrorResponse, RunRequest, RunResponse

app = FastAPI(
    title="NEXA API Gateway",
    version="0.2.0",
    description="Single external entry for NEXA v1. Versioned under /v1.",
)

# Test seam: set to an httpx transport to run the proxy without a network.
_transport: Optional[httpx.AsyncBaseTransport] = None

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid token"},
    403: {"model": ErrorResponse, "description": "Not permitted for this agent"},
    404: {"model": ErrorResponse, "description": "Unknown agent"},
    502: {"model": ErrorResponse, "description": "Runtime or model unavailable"},
}


@app.on_event("startup")
async def _startup():
    preflight("nexa-api-gateway", ["NEXA_IDENTITY_SECRET"])


app.add_middleware(TraceMiddleware, service="nexa-api-gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=config.CORS_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", TRACE_HEADER],
    expose_headers=[TRACE_HEADER],
)
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)


def _error(status: int, message: str, trace_id: Optional[str]) -> JSONResponse:
    body = {"error": {"status": status, "message": message, "trace_id": trace_id}}
    headers = {TRACE_HEADER: trace_id} if trace_id else {}
    return JSONResponse(status_code=status, content=body, headers=headers)


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    trace_id = getattr(request.state, "trace_id", None)
    return _error(exc.status_code, str(exc.detail), trace_id)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", None)
    log_event(event="error", service="nexa-api-gateway", severity="error",
              error=type(exc).__name__, trace_id=trace_id)
    return _error(500, "Internal error", trace_id)


async def _forward(
    method: str,
    path: str,
    authorization: str,
    trace_id: Optional[str] = None,
    json_body: Optional[dict[str, Any]] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    headers = {"Authorization": authorization, **(extra_headers or {})}
    if trace_id:
        headers[TRACE_HEADER] = trace_id
    url = f"{config.RUNTIME_URL}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=config.REQUEST_TIMEOUT, transport=_transport
        ) as client:
            resp = await client.request(method, url, headers=headers, json=json_body)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"runtime unreachable: {exc}")
    try:
        content = resp.json()
    except ValueError:
        content = {"detail": resp.text[:200]}
    out_headers = {TRACE_HEADER: trace_id} if trace_id else {}
    return JSONResponse(status_code=resp.status_code, content=content, headers=out_headers)


@app.get("/health")
async def health():
    """Liveness: the process is up."""
    return {"status": "ok", "service": "nexa-api-gateway", "version": app.version}


@app.get("/ready")
async def ready():
    """Readiness: the upstream runtime answers."""
    url = f"{config.RUNTIME_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0, transport=_transport) as client:
            resp = await client.get(url)
        upstream_ok = resp.status_code == 200
    except httpx.RequestError:
        upstream_ok = False
    status = "ready" if upstream_ok else "degraded"
    code = 200 if upstream_ok else 503
    return JSONResponse(status_code=code, content={
        "status": status, "service": "nexa-api-gateway", "runtime_ok": upstream_ok,
    })


@app.get("/v1/agents", response_model=AgentList, responses=_ERRORS)
async def list_agents(
    request: Request,
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    trace_id = getattr(request.state, "trace_id", None)
    return await _forward("GET", "/v1/agents", authorization, trace_id)


@app.post("/v1/agents/{name}/run", response_model=RunResponse, responses=_ERRORS)
async def run_agent(
    request: Request,
    payload: RunRequest,
    name: str = Path(..., pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    trace_id = getattr(request.state, "trace_id", None)
    audit_event(
        action=f"api.agents.run:{name}", decision="allow",
        principal=principal.subject, roles=list(principal.roles), trace_id=trace_id,
    )
    return await _forward(
        "POST", f"/v1/agents/{name}/run", authorization, trace_id,
        payload.model_dump(exclude_none=True),
    )


# Agent registry and discovery (NEXA-M3-002): served by the Agent Runtime,
# which enforces the agents.* permissions; the gateway authenticates and
# forwards. Clients discover agents by capability here instead of hard-coding
# agent names.
_REGISTRY_PATH = r"^[A-Za-z0-9._@:/-]{1,300}$"


@app.api_route("/v1/registry/{rest:path}", methods=["GET", "POST", "PUT"], responses=_ERRORS,
               include_in_schema=True)
async def registry_proxy(
    request: Request,
    rest: str = Path(..., pattern=_REGISTRY_PATH),
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    if ".." in rest:
        raise HTTPException(status_code=400, detail="Invalid path")
    trace_id = getattr(request.state, "trace_id", None)
    body = None
    if request.method in ("POST", "PUT"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="JSON body required")
    if request.method != "GET":
        audit_event(action=f"api.registry.{request.method.lower()}:{rest}", decision="allow",
                    principal=principal.subject, roles=list(principal.roles), trace_id=trace_id)
    query = f"?{request.url.query}" if request.url.query else ""
    return await _forward(request.method, f"/v1/registry/{rest}{query}", authorization, trace_id, body)


# Orchestration (NEXA-M3-003): workflows run in the Agent Runtime, which
# enforces who may start, see and decide each run. Long runs: start with
# ?wait=false and poll the run.
@app.api_route("/v1/workflows/{rest:path}", methods=["GET", "POST"], responses=_ERRORS)
@app.api_route("/v1/workflow-runs/{rest:path}", methods=["GET", "POST"], responses=_ERRORS)
async def orchestration_proxy(
    request: Request,
    rest: str = Path(..., pattern=r"^[A-Za-z0-9._/-]{0,200}$"),
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    if ".." in rest:
        raise HTTPException(status_code=400, detail="Invalid path")
    trace_id = getattr(request.state, "trace_id", None)
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="JSON body required")
        audit_event(action=f"api.orchestration.post:{request.url.path}", decision="allow",
                    principal=principal.subject, roles=list(principal.roles), trace_id=trace_id)
    query = f"?{request.url.query}" if request.url.query else ""
    extra = {"Idempotency-Key": request.headers["idempotency-key"][:200]} \
        if request.headers.get("idempotency-key") else None
    return await _forward(request.method, f"{request.url.path}{query}", authorization, trace_id, body, extra)


@app.get("/v1/workflows", responses=_ERRORS)
@app.get("/v1/workflow-runs", responses=_ERRORS)
@app.get("/v1/messaging/schema", responses=_ERRORS)
async def orchestration_list_proxy(request: Request, principal: Principal = Depends(require_principal),
                                   authorization: str = Header(...)):
    query = f"?{request.url.query}" if request.url.query else ""
    return await _forward("GET", f"{request.url.path}{query}", authorization,
                          getattr(request.state, "trace_id", None))


# Agent tool calls (NEXA-M3-004): the Agent Runtime checks the manifest grant
# and calls the Tool & Connector Gateway, which applies its own policy.
@app.post("/v1/agents/{name}/tools/{tool}", responses=_ERRORS)
async def agent_tool_proxy(
    request: Request,
    name: str = Path(..., pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
    tool: str = Path(..., pattern=r"^[a-z][a-z0-9_]{1,40}$"),
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    trace_id = getattr(request.state, "trace_id", None)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="JSON body required")
    audit_event(action=f"api.agents.tool:{name}:{tool}", decision="allow", principal=principal.subject,
                roles=list(principal.roles), trace_id=trace_id)
    return await _forward("POST", f"/v1/agents/{name}/tools/{tool}", authorization, trace_id, body)


# Session state (NEXA-M3-006): the Agent Runtime enforces who may read and
# write which state, and logs every access.
@app.api_route("/v1/sessions", methods=["POST"], responses=_ERRORS)
@app.api_route("/v1/sessions/{rest:path}", methods=["GET", "PUT", "DELETE"], responses=_ERRORS)
@app.api_route("/v1/state/{rest:path}", methods=["GET"], responses=_ERRORS)
async def state_proxy(request: Request, principal: Principal = Depends(require_principal),
                      authorization: str = Header(...)):
    path = request.url.path
    if ".." in path or not re.match(r"^/v1/(sessions|state)(/[A-Za-z0-9_./-]*)?$", path):
        raise HTTPException(status_code=400, detail="Invalid path")
    body = None
    if request.method in ("POST", "PUT"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="JSON body required")
    query = f"?{request.url.query}" if request.url.query else ""
    return await _forward(request.method, f"{path}{query}", authorization, getattr(request.state, "trace_id", None),
                          body)
