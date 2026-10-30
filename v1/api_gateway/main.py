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
) -> JSONResponse:
    headers = {"Authorization": authorization}
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
