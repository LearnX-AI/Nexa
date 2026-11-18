"""This Agent Runtime instance in the registry (NEXA-M3-002).

At start, and every NEXA_AGENT_HEARTBEAT_S seconds (default 30; 0: only at
start), the instance records a deployment for the current version of every
agent it serves, with its location and endpoint, and a heartbeat with the
health of what each agent depends on (its manifest's health.requires):

  model_gateway      GET {NEXA_GATEWAY_URL}/health
  knowledge_service  GET {NEXA_KNOWLEDGE_URL}/health   (default http://127.0.0.1:8103)
  tool_registry      every tool the agent is granted is registered and available
  memory_service     not built yet: never ready

A deployment whose heartbeat is older than NEXA_AGENT_HEARTBEAT_STALE_S (120 s)
is not offered by discovery. A version this instance no longer serves (a newer
one became current) is removed.

  NEXA_AGENT_LOCATION    core:dgx, school:pomnsoe, ... (default core:local)
  NEXA_AGENT_PUBLIC_URL  how callers reach this runtime (default http://127.0.0.1:8102)
  NEXA_AGENT_INSTANCE    a stable name for this instance (default host:port of the URL)
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from . import config, registry, tools

log = logging.getLogger("nexa.agent_runtime.presence")


def location() -> str:
    return os.environ.get("NEXA_AGENT_LOCATION", "core:local")


def endpoint() -> str:
    return os.environ.get("NEXA_AGENT_PUBLIC_URL", "http://127.0.0.1:8102").rstrip("/")


def instance() -> str:
    if os.environ.get("NEXA_AGENT_INSTANCE"):
        return os.environ["NEXA_AGENT_INSTANCE"]
    u = urlparse(endpoint())
    return f"{socket.gethostname()}:{u.port or 8102}"


def actor() -> str:
    return f"runtime:{instance()}"


async def _probe(url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
        return {"ok": r.status_code == 200, "status": r.status_code}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}


async def dependency_checks() -> dict[str, dict[str, Any]]:
    knowledge = os.environ.get("NEXA_KNOWLEDGE_URL", "http://127.0.0.1:8103").rstrip("/")
    gw, kn = await asyncio.gather(_probe(f"{config.GATEWAY_URL}/health"), _probe(f"{knowledge}/health"))
    return {"model_gateway": gw, "knowledge_service": kn,
            "memory_service": {"ok": False, "error": "not built (NEXA-M3 Memory Service)"}}


def agent_health(spec: registry.AgentSpec, requires: list[str], deps: dict[str, dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for need in requires:
        if need == "tool_registry":
            missing = [t for t in spec.tools if t not in tools.TOOLS or not tools.TOOLS[t].available]
            checks[need] = {"ok": not missing, **({"unavailable": missing} if missing else {})}
        else:
            checks[need] = deps.get(need, {"ok": False, "error": "unknown dependency"})
    return {"ready": all(c["ok"] for c in checks.values()), "checks": checks}


async def beat() -> list[dict[str, Any]]:
    """Record this instance's deployments and their health once."""
    reg = registry.REGISTRY
    if reg is None:
        return []
    deps = await dependency_checks()
    inst, loc, url, who = instance(), location(), endpoint(), actor()
    served, out = set(), []
    for agent_id in reg.agent_ids():
        spec = reg.spec(agent_id)
        m = reg.manifest(agent_id, spec.version) if spec else None
        if spec is None or m is None:
            continue
        dep_id = reg.register_deployment(agent_id, spec.version, location=loc, endpoint=url, instance=inst,
                                         actor=who)["deployment_id"]
        health = agent_health(spec, list(m.health.requires), deps)
        reg.heartbeat(dep_id, health, who)
        served.add(dep_id)
        out.append({"deployment_id": dep_id, **health})
    for r in reg.store.query("SELECT deployment_id FROM deployments WHERE instance = ?", (inst,)):
        if r["deployment_id"] not in served:
            reg.deregister_deployment(r["deployment_id"], who)
    return out


async def run_forever() -> None:
    period = float(os.environ.get("NEXA_AGENT_HEARTBEAT_S", "30"))
    while period > 0:
        await asyncio.sleep(period)
        try:
            await beat()
        except Exception:  # noqa: BLE001 - a failed beat must not stop the runtime
            log.exception("agent registry heartbeat failed")
