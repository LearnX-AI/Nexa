"""Tool invocation boundary of the Agent Runtime (NEXA-ADR-0003, ADR-0006, NEXA-M3-004).

Agents reach tools only through the Tool & Connector Gateway, and only through
here. invoke_tool() checks, in the runtime:

  - the agent exists, is runnable and enabled (registry, M3-002);
  - the agent's manifest grants the tool (M3-001), and the user holds the
    permission the manifest names;
  - the per-run cap: max_calls_per_run from the manifest, counted per
    workflow run;

then calls the gateway with the runtime's service token, the user's own token
and the agent's identity. The gateway applies its own checks (allowlist,
scopes, arguments, data, rate limits, timeout) and audits every call.

TOOLS is the catalog the manifests are validated against: the tool contracts
in v1/tool_gateway/tools/. A tool whose contract is not active is unavailable,
and a runnable agent may not be granted it.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.identity.tokens import issue_token
from v1.observability import TRACE_HEADER, audit_event, span

from . import config


class ToolError(Exception):
    def __init__(self, status_code: int, detail: str, call_id: Optional[str] = None):
        self.status_code = status_code
        self.detail = detail
        self.call_id = call_id
        super().__init__(detail)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_permission: str = "model.invoke"
    available: bool = True
    version: str = "0.0.0"


TOOLS: dict[str, ToolSpec] = {}
CONTRACTS: dict = {}            # tool id -> its nexa.tool/1.0 contract (scopes, side effects)
DELEGATION_TTL_S = 120


def load_catalog(directory: Optional[str] = None) -> dict[str, ToolSpec]:
    """The tool contracts, read as data (the runtime never runs a tool itself)."""
    from v1.tool_gateway import contract as ct
    tools, _ = ct.load(directory or os.environ.get("NEXA_TOOL_DIR") or ct.TOOLS_DIR,
                       os.environ.get("NEXA_CONNECTOR_DIR") or ct.CONNECTORS_DIR)
    TOOLS.clear()
    CONTRACTS.clear()
    CONTRACTS.update(tools)
    for t in tools.values():
        TOOLS[t.id] = ToolSpec(t.id, t.description, t.permission, t.state == "active", t.version)
    return TOOLS


load_catalog()

GATEWAY_URL = os.environ.get("NEXA_TOOL_GATEWAY_URL", "http://127.0.0.1:8105").rstrip("/")
# Tests replace this with an in-process transport to the gateway app.
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

_RUN_CALLS: dict[tuple[str, str, str], int] = {}
_RUN_LOCK = threading.Lock()


def _count(run_id: Optional[str], agent: str, tool: str, limit: int) -> bool:
    if not run_id:
        return True
    with _RUN_LOCK:
        k = (run_id, agent, tool)
        if _RUN_CALLS.get(k, 0) >= limit:
            return False
        _RUN_CALLS[k] = _RUN_CALLS.get(k, 0) + 1
        if len(_RUN_CALLS) > 50000:          # bounded memory: forget the oldest runs
            for old in list(_RUN_CALLS)[:10000]:
                _RUN_CALLS.pop(old, None)
        return True


async def invoke_tool(agent_name: str, tool_name: str, principal: Principal, args: Optional[dict] = None, *,
                      user_token: Optional[str] = None, scopes: Optional[list[str]] = None,
                      run_id: Optional[str] = None, step: Optional[str] = None,
                      trace_id: Optional[str] = None) -> dict[str, Any]:
    from . import registry   # here, not at import: the registry validates manifests against TOOLS
    action = f"tool.invoke:{agent_name}:{tool_name}"

    def deny(status: int, detail: str, **why):
        audit_event(action=action, decision="deny", principal=principal.subject, trace_id=trace_id,
                    run_id=run_id, **why)
        return ToolError(status, detail)

    agent = registry.get_agent(agent_name)
    if agent is None:
        raise deny(404, f"Unknown agent: {agent_name}", reason="unknown agent")
    if not agent.enabled:
        raise deny(423, f"Agent {agent_name} is disabled", reason="agent disabled")
    if not agent.runnable:
        raise deny(409, f"Agent {agent_name} is not active (state: {agent.state})", reason="agent not runnable")
    if agent.allowed_roles and not set(principal.roles) & set(agent.allowed_roles):
        raise deny(403, "Not permitted", reason="role may not use this agent")
    m = registry.MANIFESTS.get(agent_name)
    grant = next((g for g in (m.tools if m else []) if g.name == tool_name), None)
    spec = TOOLS.get(tool_name)
    if grant is None or spec is None:
        raise deny(403, "Tool not permitted for this agent", reason="tool not granted in the manifest")
    if not can(principal, grant.permission):
        raise deny(403, "Not permitted", permission=grant.permission)
    if not spec.available:
        raise deny(503, f"Tool {tool_name} is not available", reason="tool not active")
    if not user_token:
        raise deny(401, "A tool call needs the user's own session", reason="no user token")
    # The agent's least privilege (NEXA-M3-007): its policy decides the scopes.
    from .policy import pdp
    decision, scopes = pdp.authorize_invoke(agent_name, agent.version, tool_name, scopes, principal,
                                            trace_id=trace_id, run_id=run_id)
    if not decision.allow:
        raise deny(decision.status, decision.reason, reason=f"policy:{decision.rule}",
                   decision_id=decision.decision_id)
    if not _count(run_id, agent_name, tool_name, grant.max_calls_per_run):
        raise deny(429, f"{agent_name} may call {tool_name} at most {grant.max_calls_per_run} times in one run",
                   reason="max_calls_per_run")
    audit_event(action=action, decision="allow", principal=principal.subject, permission=grant.permission,
                trace_id=trace_id, run_id=run_id)
    # Not the user's own token: a delegated one, naming this agent, with only
    # the scopes just decided, for the Tool Gateway, for two minutes.
    delegated = issue_token(principal.subject, list(principal.roles), "delegated", DELEGATION_TTL_S, extra={
        **({"school_id": principal.school_id} if principal.school_id else {}),
        **({"class_ids": principal.attrs["class_ids"]} if principal.attrs.get("class_ids") else {}),
        "act": pdp.identity(agent_name, agent.version), "scope": scopes, "nexa_aud": "nexa-tool-gateway",
        **({"run_id": run_id} if run_id else {})})
    headers = {"Authorization": f"Bearer {config.GATEWAY_TOKEN}", "X-NEXA-On-Behalf-Of": f"Bearer {delegated}",
               "X-NEXA-Agent": agent_name, "X-NEXA-Agent-Version": agent.version}
    if run_id:
        headers["X-NEXA-Run"] = run_id
    if step:
        headers["X-NEXA-Step"] = step
    if trace_id:
        headers[TRACE_HEADER] = trace_id
    body = {"arguments": args or {}, "scopes": scopes}
    with span(f"tool.{tool_name}", trace_id, agent=agent_name):
        try:
            async with httpx.AsyncClient(timeout=200.0, transport=TRANSPORT) as client:
                r = await client.post(f"{GATEWAY_URL}/v1/tools/{tool_name}/invoke", json=body, headers=headers)
        except httpx.TimeoutException:
            raise ToolError(504, "tool gateway timed out") from None
        except httpx.HTTPError as exc:
            raise ToolError(503, f"tool gateway unreachable: {type(exc).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text[:200]}
    if r.status_code != 200:
        raise ToolError(r.status_code, str(data.get("detail", "tool call failed")), data.get("call_id"))
    return data
