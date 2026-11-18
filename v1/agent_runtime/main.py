"""NEXA Agent Runtime service (M1 skeleton). NEXA-ADR-0003.

Run:
    export NEXA_IDENTITY_SECRET=...            # shared with identity and gateway
    export NEXA_AGENT_GATEWAY_TOKEN=...        # service token for the gateway
    uvicorn v1.agent_runtime.main:app --host 127.0.0.1 --port 8102
"""
from __future__ import annotations
import asyncio

from fastapi import FastAPI, Depends, HTTPException, Request

from v1.identity.principal import Principal
from v1.observability import TraceMiddleware, metrics_endpoint
from v1.shared.config import preflight

from . import manifest, presence, registry
from .registry_api import router as registry_router
from .auth import require_principal
from .runtime import run_agent, AgentRuntimeError
from .schemas import RunRequest, RunResponse

app = FastAPI(title="NEXA Agent Runtime", version="0.1.0")
app.add_middleware(TraceMiddleware, service="nexa-agent-runtime")
app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
app.include_router(registry_router)
_tasks: list[asyncio.Task] = []


@app.on_event("startup")
async def _startup():
    preflight("nexa-agent-runtime", ["NEXA_IDENTITY_SECRET", "NEXA_AGENT_GATEWAY_TOKEN"])
    # Record this instance's deployments in the registry (NEXA-M3-002).
    try:
        await presence.beat()
    except Exception:  # noqa: BLE001 - the runtime still serves; discovery shows it as missing
        presence.log.exception("agent registry: first heartbeat failed")
    _tasks.append(asyncio.create_task(presence.run_forever()))


@app.on_event("shutdown")
async def _shutdown():
    for t in _tasks:
        t.cancel()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nexa-agent-runtime",
        "version": "0.1.0",
        "agents": list(registry.AGENTS),
        "agent_states": {a.name: a.state for a in registry.AGENTS.values()},
        "manifest_version": manifest.MANIFEST_VERSION,
        "registry": {"location": presence.location(), "instance": presence.instance(),
                     "disabled": sorted(a.name for a in registry.AGENTS.values() if not a.enabled)},
    }


@app.get("/v1/agents")
async def list_agents(principal: Principal = Depends(require_principal)):
    return {
        "agents": [
            {
                "name": a.name,
                "description": a.description,
                "model": a.model,
                "version": a.version,
                "deprecated": a.deprecated,
                "deprecated_since": a.deprecated_since,
                "replaced_by": a.replaced_by,
                "required_permission": a.required_permission,
                "capabilities": list(a.capabilities),
                "tools": list(a.tools),
                "state": a.state,
                "runnable": a.runnable,
                "allowed_roles": list(a.allowed_roles),
                "sunset_on": a.sunset_on,
                "manifest": a.fingerprint,
                "enabled": a.enabled,
            }
            for a in registry.AGENTS.values()
        ]
    }


@app.get("/v1/agents/{name}/manifest")
async def get_manifest(name: str, principal: Principal = Depends(require_principal)):
    """The agent's full manifest (nexa.agent/1.0) and its fingerprint: what was
    reviewed is what runs."""
    m = registry.MANIFESTS.get(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}")
    return {"manifest": m.model_dump(mode="json"), "fingerprint": m.fingerprint()}


@app.post("/v1/agents/{name}/run", response_model=RunResponse)
async def run(
    name: str,
    req: RunRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    messages = [m.model_dump() for m in req.messages]
    trace_id = getattr(request.state, "trace_id", None)
    try:
        return await run_agent(
            name, principal, messages, req.temperature, req.max_tokens, trace_id
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
