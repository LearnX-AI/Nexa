"""Core agent execution (NEXA-ADR-0003).

Enforces the caller's permission for the agent, prepends the agent system
prompt, calls the gateway, and returns the output with a trace block. Tool
calling and multi step planning are later phase work and are not here.
"""
from __future__ import annotations
import time
from typing import Any, Optional

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import new_trace_id, audit_event

from . import registry, gateway_client
from .schemas import RunResponse


class AgentRuntimeError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def run_agent(
    name: str,
    principal: Principal,
    messages: list[dict[str, Any]],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> RunResponse:
    trace_id = trace_id or new_trace_id()
    agent = registry.get_agent(name)
    if agent is None:
        raise AgentRuntimeError(404, f"Unknown agent: {name}")
    if agent.deprecated:
        hint = f"; use {agent.replaced_by}" if agent.replaced_by else ""
        raise AgentRuntimeError(410, f"Agent {name} is deprecated{hint}")
    if not can(principal, agent.required_permission):
        audit_event(
            action=f"agent.run:{name}", decision="deny", principal=principal.subject,
            permission=agent.required_permission, trace_id=trace_id,
        )
        raise AgentRuntimeError(403, "Not permitted")
    audit_event(
        action=f"agent.run:{name}", decision="allow", principal=principal.subject,
        permission=agent.required_permission, trace_id=trace_id,
    )

    started = time.time()
    full = [{"role": "system", "content": agent.system_prompt}] + messages

    try:
        data = await gateway_client.chat(
            agent.model, full, temperature, max_tokens, trace_id
        )
    except gateway_client.GatewayError as exc:
        raise AgentRuntimeError(502, str(exc))

    output = ""
    try:
        output = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        output = ""

    x_nexa = data.get("x_nexa") if isinstance(data, dict) else None
    if not isinstance(x_nexa, dict):
        x_nexa = {}
    x_nexa.setdefault("runtime", "agent-runtime")
    x_nexa["agent"] = name
    x_nexa["agent_version"] = agent.version
    x_nexa["trace_id"] = trace_id
    x_nexa["latency_ms"] = int((time.time() - started) * 1000)
    x_nexa["principal"] = principal.subject

    usage = data.get("usage") if isinstance(data, dict) else None
    return RunResponse(
        agent=name,
        agent_version=agent.version,
        model=agent.model,
        output=output,
        trace_id=trace_id,
        usage=usage if isinstance(usage, dict) else None,
        x_nexa=x_nexa,
    )
