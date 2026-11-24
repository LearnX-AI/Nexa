"""Core agent execution (NEXA-ADR-0003).

Enforces the caller's permission for the agent, prepends the agent system
prompt, calls the gateway, and returns the output with a trace block. Tool
calling and multi step planning are later phase work and are not here.
"""
from __future__ import annotations
import time
from typing import Any, Optional

from v1.identity.principal import Principal
from v1.observability import new_trace_id, audit_event

import asyncio
import contextvars

from . import registry, gateway_client
from . import pedagogy
from .policy import pdp
from .schemas import RunResponse


class AgentRuntimeError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# The agent run in progress in this task, if any. An agent never calls another
# agent: anything running inside an agent run (a tool, a callback) that tries
# to start one is refused. Agents are composed only by the orchestration
# engine, which runs them one after another, never nested (NEXA-M3-003).
_ACTIVE_RUN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("nexa_active_agent_run", default=None)


def active_run() -> Optional[str]:
    return _ACTIVE_RUN.get()


async def run_agent(
    name: str,
    principal: Principal,
    messages: list[dict[str, Any]],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    trace_id: Optional[str] = None,
    caller: Optional[dict[str, Any]] = None,
    teaching: Optional[dict[str, Any]] = None,
) -> RunResponse:
    """Run one agent. caller: who is running it, recorded in the trace (the
    orchestration engine passes the workflow, run and step). teaching: the
    learner's grade and whether the output is internal to the orchestration
    ({"grade": "11", "internal": True}), for the Teaching Intelligence Policy."""
    trace_id = trace_id or new_trace_id()
    outer = _ACTIVE_RUN.get()
    if outer is not None:
        audit_event(action=f"agent.run:{name}", decision="deny", principal=principal.subject,
                    reason=f"agent-to-agent call from {outer}", trace_id=trace_id)
        raise AgentRuntimeError(409, f"Agent {name} cannot be called from inside agent {outer}: agents are "
                                     f"composed only by the orchestration engine")
    token = _ACTIVE_RUN.set(name)
    try:
        return await _run_agent(name, principal, messages, temperature, max_tokens, trace_id, caller,
                                teaching or {})
    finally:
        _ACTIVE_RUN.reset(token)


def teaching_block(agent, principal: Principal, messages: list[dict[str, Any]], teaching: dict[str, Any]):
    """(screen, composed) for an agent under the Teaching Intelligence Policy
    (NEXA-M4-001), or (None, None). The policy text is added to the agent's
    system prompt; an escalation replaces the model call."""
    if not agent.pedagogy_version:
        return None, None
    policy = pedagogy.get(agent.pedagogy_version)
    internal = bool(teaching.get("internal"))
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    scr = {} if internal else pedagogy.screen(policy, last_user)
    composed = pedagogy.compose(policy, profile="internal" if internal else agent.pedagogy_profile,
                                roles=principal.roles, grade=teaching.get("grade"), language=scr.get("language"))
    return scr, composed


async def _run_agent(name, principal, messages, temperature, max_tokens, trace_id, caller, teaching) -> RunResponse:
    agent = registry.get_agent(name)
    if agent is None:
        raise AgentRuntimeError(404, f"Unknown agent: {name}")
    hint = f"; use {agent.replaced_by}" if agent.replaced_by else ""
    if not agent.enabled:
        # The registry's kill switch (NEXA-M3-002): refused whatever the state.
        raise AgentRuntimeError(423, f"Agent {name} is disabled: {agent.disabled_reason or 'no reason given'}")
    if not agent.runnable:
        # Lifecycle (NEXA-M3-001): only active agents, and deprecated ones
        # until their sunset date, run.
        if agent.state in ("retired",) or agent.past_sunset:
            raise AgentRuntimeError(410, f"Agent {name} is retired{hint}")
        if agent.state == "suspended":
            raise AgentRuntimeError(423, f"Agent {name} is suspended")
        raise AgentRuntimeError(409, f"Agent {name} is not active (state: {agent.state})")
    # The agent policy decision point (NEXA-M3-007): default deny, role,
    # permission; recorded with the rule that decided.
    decision = pdp.authorize_run(agent, principal, trace_id=trace_id, run_id=(caller or {}).get("run_id"))
    if not decision.allow:
        audit_event(
            action=f"agent.run:{name}", decision="deny", principal=principal.subject,
            permission=agent.required_permission, trace_id=trace_id, rule=decision.rule,
        )
        raise AgentRuntimeError(decision.status, "Not permitted")
    audit_event(
        action=f"agent.run:{name}", decision="allow", principal=principal.subject,
        permission=agent.required_permission, trace_id=trace_id,
        **({"caller": caller} if caller else {}),
    )

    started = time.time()
    scr, composed = teaching_block(agent, principal, messages, teaching)
    if scr and scr.get("escalation"):
        # A learner in distress or danger gets the policy's response and a
        # person to talk to, never a model's answer (TP-ESC-01). Ids only.
        audit_event(action=f"pedagogy.escalation:{name}", decision="allow", principal=principal.subject,
                    trigger=scr["escalation"], rule=scr["rule"], policy=composed.record["version"],
                    trace_id=trace_id, **({"run_id": caller.get("run_id")} if caller else {}))
        policy = pedagogy.get(agent.pedagogy_version)
        return RunResponse(agent=name, agent_version=agent.version, model="none",
                           output=policy.escalation_response[composed.record["audience"]], trace_id=trace_id,
                           x_nexa={"runtime": "agent-runtime", "agent": name, "agent_version": agent.version,
                                   "trace_id": trace_id, "attempts": 0, "pedagogy": composed.record,
                                   "escalation": scr["escalation"], **({"caller": caller} if caller else {}),
                                   **({"manifest": agent.fingerprint} if agent.fingerprint else {})})
    system = agent.system_prompt + ("\n\n" + composed.text if composed else "")
    full = [{"role": "system", "content": system}] + messages
    temperature = agent.temperature if temperature is None else temperature
    if agent.max_output_tokens:
        max_tokens = min(max_tokens or agent.max_output_tokens, agent.max_output_tokens)
    data, attempts = await _call_with_policy(agent, full, temperature, max_tokens, trace_id)

    output = ""
    try:
        output = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        output = ""
    output = pdp.redact(name, agent.version, output, principal, trace_id=trace_id,
                        run_id=(caller or {}).get("run_id"))

    x_nexa = data.get("x_nexa") if isinstance(data, dict) else None
    if not isinstance(x_nexa, dict):
        x_nexa = {}
    x_nexa.setdefault("runtime", "agent-runtime")
    x_nexa["agent"] = name
    x_nexa["agent_version"] = agent.version
    x_nexa["trace_id"] = trace_id
    x_nexa["latency_ms"] = int((time.time() - started) * 1000)
    x_nexa["principal"] = principal.subject
    x_nexa["attempts"] = attempts
    if caller:
        x_nexa["caller"] = caller
    if agent.fingerprint:
        x_nexa["manifest"] = agent.fingerprint
    if composed:
        x_nexa["pedagogy"] = composed.record
    if agent.state == "deprecated" or agent.deprecated:
        x_nexa["deprecation"] = {"deprecated_on": agent.deprecated_since, "sunset_on": agent.sunset_on,
                                 "replaced_by": agent.replaced_by}

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


async def _call_with_policy(agent, messages, temperature, max_tokens, trace_id):
    """The manifest's runtime policy: the whole run within timeout_s; a retry,
    after backoff_s, only for the failure kinds in retry_on; and, when the
    model stays unavailable, the declared fallback models in order."""
    deadline = time.monotonic() + agent.timeout_s
    attempts = 0
    last: Optional[str] = None
    for model in (agent.model, *agent.fallbacks):
        tries = 0
        while True:
            attempts += 1
            tries += 1
            left = deadline - time.monotonic()
            if left <= 0:
                raise AgentRuntimeError(504, f"Agent {agent.name} timed out after {agent.timeout_s:.0f} s")
            try:
                data = await asyncio.wait_for(
                    gateway_client.chat(model, messages, temperature, max_tokens, trace_id), timeout=left)
                if isinstance(data, dict) and model != agent.model:
                    data.setdefault("x_nexa", {})["fallback_model"] = model
                return data, attempts
            except asyncio.TimeoutError:
                kind, last = "timeout", f"{model} timed out"
            except gateway_client.GatewayError as e:
                kind, last = getattr(e, "kind", "error"), str(e)
            if kind == "error":                      # a real error: neither retried nor sent to a fallback
                raise AgentRuntimeError(502, last)
            retry_kind = {"unavailable": "gateway_unavailable", "timeout": "timeout"}[kind]
            if tries >= agent.max_attempts or retry_kind not in agent.retry_on \
                    or time.monotonic() + agent.backoff_s >= deadline:
                break                                # next fallback model, if any
            await asyncio.sleep(agent.backoff_s)
    if last and "timed out" in last:
        raise AgentRuntimeError(504, f"Agent {agent.name} timed out")
    raise AgentRuntimeError(502, last or "model unavailable")
