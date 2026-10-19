"""NEXA API Gateway service (M1 skeleton). NEXA-ARCH-001.

The single external entry. Verifies the caller at the edge, applies CORS, and
proxies to the Agent Runtime. Caller tokens are forwarded unchanged; the runtime
enforces per agent RBAC.

Run:
    export NEXA_IDENTITY_SECRET=...        # shared, to verify callers
    uvicorn v1.api_gateway.main:app --host 127.0.0.1 --port 8099
"""
from __future__ import annotations
from typing import Any, Optional
import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from v1.identity.principal import Principal

from . import config
from .auth import require_principal

app = FastAPI(title="NEXA API Gateway", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=config.CORS_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


async def _forward(
    method: str,
    path: str,
    authorization: str,
    json_body: Optional[dict[str, Any]] = None,
) -> JSONResponse:
    url = f"{config.RUNTIME_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT) as client:
            resp = await client.request(
                method, url, headers={"Authorization": authorization}, json=json_body
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"runtime unreachable: {exc}")
    try:
        content = resp.json()
    except ValueError:
        content = {"detail": resp.text[:200]}
    return JSONResponse(status_code=resp.status_code, content=content)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexa-api-gateway", "version": "0.1.0"}


@app.get("/v1/agents")
async def list_agents(
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    return await _forward("GET", "/v1/agents", authorization)


@app.post("/v1/agents/{name}/run")
async def run_agent(
    name: str,
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_principal),
    authorization: str = Header(...),
):
    return await _forward("POST", f"/v1/agents/{name}/run", authorization, payload)
