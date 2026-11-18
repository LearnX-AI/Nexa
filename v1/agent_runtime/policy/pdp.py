"""The agent policy decision point (NEXA-M3-007).

One place decides what an agent may do for a user; the enforcement points
call it and act on the decision:

  run          Agent Runtime, before an agent runs
  invoke       Agent Runtime, before a tool call goes to the Tool Gateway
  delegate     orchestration engine, before a coordinator's choice is sent
  decide       orchestration engine, before a person decides a checkpoint
  view         orchestration engine, before a run is shown to a reviewer
  redact       Agent Runtime, on the output of an agent that must redact

Every decision, allowed or denied, is one row in policy_decisions and one
audit event: the agent and version (its service identity, agent:<id>@<v>),
the user, roles and school (the tenant), the action and resource, the
decision, the rule that decided and why, the trace and run.

Rules are default deny and checked in order; the first that fails decides.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import audit_event

from . import contract as pc

POLICIES: dict[str, pc.AgentPolicy] = {}
OVERSIGHT_ROLES = {"admin", "platform_admin", "national_admin"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?675[ -]?)?7\d{3}[ -]?\d{4}(?!\d)")


@dataclass
class Decision:
    allow: bool
    rule: str
    reason: str
    status: int = 200
    decision_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def load(directory: Optional[str] = None, *, strict: bool = False) -> dict[str, pc.AgentPolicy]:
    """Load and check the policies. A policy for an agent this registry does
    not have grants nothing and is skipped, unless strict (CI: every shipped
    policy must name a shipped agent)."""
    from .. import registry, tools
    found = pc.load(directory or pc.POLICY_DIR)
    if not strict:
        found = {a: p for a, p in found.items() if a in registry.MANIFESTS}
    pc.validate_all(found, dict(registry.MANIFESTS), tools.CONTRACTS, directory)
    POLICIES.clear()
    POLICIES.update(found)
    return found


def identity(agent: str, version: Optional[str]) -> str:
    """The agent's service identity in decisions, tokens and audit."""
    return f"agent:{agent}@{version or '?'}"


def tenant_of(p: Principal) -> Optional[str]:
    return p.school_id


def _record(action: str, agent: Optional[str], version: Optional[str], principal: Principal, resource: str,
            d: Decision, trace_id: Optional[str], run_id: Optional[str]) -> Decision:
    from .. import registry
    d.decision_id = "pd_" + uuid.uuid4().hex[:20]
    if registry.REGISTRY is not None:
        with registry.REGISTRY.store.tx() as db:
            db.execute("INSERT INTO policy_decisions (decision_id, ts, agent, agent_version, actor, roles, tenant, "
                       "action, resource, decision, rule, reason, trace_id, run_id, detail) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (d.decision_id, time.time(), agent, version, principal.subject,
                        json.dumps(list(principal.roles)), tenant_of(principal), action, resource,
                        "allow" if d.allow else "deny", d.rule, d.reason, trace_id, run_id,
                        json.dumps(d.detail, default=str)))
    audit_event(action=f"policy.{action}:{agent or '-'}", decision="allow" if d.allow else "deny",
                principal=principal.subject, agent=identity(agent, version) if agent else None,
                tenant=tenant_of(principal), resource=resource, rule=d.rule, reason=d.reason,
                decision_id=d.decision_id, trace_id=trace_id, run_id=run_id)
    return d


def decisions(*, agent: Optional[str] = None, action: Optional[str] = None, decision: Optional[str] = None,
              trace_id: Optional[str] = None, run_id: Optional[str] = None, actor: Optional[str] = None,
              limit: int = 200) -> list[dict[str, Any]]:
    from .. import registry
    where, args = [], []
    for col, v in (("agent", agent), ("action", action), ("decision", decision), ("trace_id", trace_id),
                   ("run_id", run_id), ("actor", actor)):
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    sql = "SELECT * FROM policy_decisions" + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY seq DESC LIMIT ?"
    rows = registry.REGISTRY.store.query(sql, (*args, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["roles"] = json.loads(d["roles"] or "[]")
        d["detail"] = json.loads(d["detail"] or "{}")
        out.append(d)
    return out


def _policy(agent: str) -> Optional[pc.AgentPolicy]:
    return POLICIES.get(agent)


# ---------------------------------------------------------------- decisions

def authorize_run(spec, principal: Principal, *, trace_id=None, run_id=None) -> Decision:
    """An agent may run for this user: it has a policy, the user's role is one
    the manifest allows, and the user holds the run permission."""
    res = f"agent:{spec.name}"
    if _policy(spec.name) is None:
        d = Decision(False, "default_deny", f"{spec.name} has no agent policy", 403)
    elif spec.allowed_roles and not set(principal.roles) & set(spec.allowed_roles):
        d = Decision(False, "role", f"role {','.join(principal.roles) or 'none'} may not use {spec.name}", 403)
    elif not can(principal, spec.required_permission):
        d = Decision(False, "permission", f"the user does not hold {spec.required_permission}", 403)
    else:
        d = Decision(True, "allowed", "role and permission")
    return _record("run", spec.name, spec.version, principal, res, d, trace_id, run_id)


def authorize_invoke(agent: str, version: Optional[str], tool: str, requested: Optional[list[str]],
                     principal: Principal, *, trace_id=None, run_id=None) -> tuple[Decision, list[str]]:
    """A tool call for an agent. Effective scopes: those asked for, or the
    tool's default scopes, each of which the agent's policy must list for the
    tool, whose knowledge class the policy must allow, and which a student may
    never use to reach teacher-only material."""
    from .. import tools
    res = f"tool:{tool}"
    p, tc = _policy(agent), tools.CONTRACTS.get(tool)
    scopes: list[str] = []
    if p is None:
        d = Decision(False, "default_deny", f"{agent} has no agent policy", 403)
    elif tc is None:
        d = Decision(False, "tool", f"unknown tool {tool}", 404)
    elif tool not in p.invoke:
        d = Decision(False, "invoke", f"{agent}'s policy does not let it invoke {tool}", 403)
    else:
        scopes = list(requested) if requested else [s for s in tc.default_scopes if s in p.invoke[tool]]
        outside = [s for s in scopes if s not in p.invoke[tool]]
        classes = {pc.SCOPE_CLASS[s] for s in scopes if s in pc.SCOPE_CLASS}
        if not scopes:
            d = Decision(False, "scopes", f"no scope of {tool} is granted to {agent}", 403)
        elif outside:
            d = Decision(False, "scopes", f"{agent} may not use {outside} on {tool}", 403)
        elif classes - set(p.read.knowledge_classes):
            d = Decision(False, "read", f"{agent} may not read {sorted(classes - set(p.read.knowledge_classes))}",
                         403)
        elif "teacher_only" in classes and set(principal.roles) <= {"student"}:
            d = Decision(False, "sensitive", "teacher-only material is never retrieved for a student", 403)
        elif tc.side_effects == "write" and tool not in p.write:
            d = Decision(False, "write", f"{agent}'s policy does not let it use {tool}, which writes", 403)
        else:
            d = Decision(True, "allowed", "within the agent's policy", detail={"scopes": scopes})
    return _record("invoke", agent, version, principal, res, d, trace_id, run_id), scopes


def authorize_delegate(coordinator: str, version: Optional[str], target: str, principal: Principal, *,
                       trace_id=None, run_id=None) -> Decision:
    p, t = _policy(coordinator), _policy(target)
    if p is None:
        d = Decision(False, "default_deny", f"{coordinator} has no agent policy", 403)
    elif target not in p.delegate:
        d = Decision(False, "delegate", f"{coordinator}'s policy does not let it delegate to {target}", 403)
    elif t is None or not t.accept_delegation:
        d = Decision(False, "accept_delegation", f"{target} does not accept delegated work", 403)
    else:
        d = Decision(True, "allowed", "listed in the coordinator's policy")
    return _record("delegate", coordinator, version, principal, f"agent:{target}", d, trace_id, run_id)


def same_tenant(initiator_school: Optional[str], principal: Principal) -> bool:
    """A school-scoped run is reviewed and seen only within its school, except
    by national oversight roles."""
    if not initiator_school or set(principal.roles) & OVERSIGHT_ROLES:
        return True
    return principal.school_id == initiator_school


def authorize_tenant(action: str, initiator_school: Optional[str], principal: Principal, resource: str, *,
                     trace_id=None, run_id=None) -> Decision:
    if same_tenant(initiator_school, principal):
        d = Decision(True, "allowed", "same school" if initiator_school else "not school-scoped")
    else:
        d = Decision(False, "tenancy", "the run belongs to another school", 403)
    return _record(action, None, None, principal, resource, d, trace_id, run_id)


def redact(agent: str, version: Optional[str], text: str, principal: Principal, *, trace_id=None,
           run_id=None) -> str:
    p = _policy(agent)
    if not text or p is None or not p.sensitive.redact_personal_data:
        return text
    found = len(EMAIL.findall(text)) + len(PHONE.findall(text))
    if not found:
        return text
    out = PHONE.sub("[phone removed]", EMAIL.sub("[email removed]", text))
    _record("redact", agent, version, principal, "output", Decision(True, "sensitive", f"{found} item(s) of "
                                                                    "personal data removed from the output",
                                                                    detail={"items": found}), trace_id, run_id)
    return out
