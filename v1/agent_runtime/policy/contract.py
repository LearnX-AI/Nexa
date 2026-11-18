"""Agent policies: nexa.agent-policy/1.0 (NEXA-M3-007).

What each agent may do, as its least privilege: a reviewed file per agent in
v1/agent_runtime/policy/agents/. Default deny: an agent without a policy can
do nothing; anything a policy does not list is refused.

  read       knowledge access classes the agent may receive (learner,
             teacher_only); tool scopes map onto them (SCOPE_CLASS)
  write      targets with side effects the agent may use (tools whose
             contract says side_effects: write); none today
  invoke     tool -> the scopes it may use; the least privilege, inside what
             the manifest grants and the Tool Gateway's allowlist allows
  delegate   agents it may hand work to as a coordinator (M3-003)
  accept_delegation   whether a coordinator may hand work to it
  tenancy    user_school: the agent acts only in the calling user's school
             context (tokens it carries, reviews of its work)
  sensitive  redact_personal_data: email addresses and phone numbers are
             removed from its output; required for agents serving minors

A policy never grants more than the agent's manifest declares (check()):
invoked tools must be granted by the manifest, read classes must be within
the manifest's knowledge access classes, and an agent that serves minors
must redact personal data.
"""
from __future__ import annotations

import json
import os
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

POLICY_VERSION = "nexa.agent-policy/1.0"
POLICY_DIR = os.path.join(os.path.dirname(__file__), "agents")
# Which knowledge access class a tool scope reaches. Scopes not listed reach
# no knowledge (calculator: compute).
SCOPE_CLASS = {"curriculum.learner": "learner", "curriculum.teacher": "teacher_only"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Read(Strict):
    knowledge_classes: list[Literal["learner", "teacher_only", "restricted"]] = Field(default_factory=list)


class Sensitive(Strict):
    redact_personal_data: bool = True


class AgentPolicy(Strict):
    policy_version: Literal["nexa.agent-policy/1.0"]
    agent: str
    owner: str = Field(min_length=2)
    rationale: str = Field(min_length=10, max_length=1000, description="Why the agent needs what it is granted.")
    read: Read = Field(default_factory=Read)
    write: list[str] = Field(default_factory=list)
    invoke: dict[str, list[str]] = Field(default_factory=dict)
    delegate: list[str] = Field(default_factory=list)
    accept_delegation: bool = False
    tenancy: Literal["user_school"] = "user_school"
    sensitive: Sensitive = Field(default_factory=Sensitive)

    def fingerprint(self) -> str:
        import hashlib
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


class PolicyError(Exception):
    def __init__(self, path: str, problems: list[str]):
        self.path, self.problems = path, problems
        super().__init__(f"{path}: " + "; ".join(problems))


def check(p: AgentPolicy, manifests: dict, tool_contracts: dict, policies: dict) -> list[str]:
    """A policy within its manifest and the tool contracts."""
    problems: list[str] = []
    m = manifests.get(p.agent)
    if m is None:
        return [f"{p.agent} is not a registered agent"]
    granted = {t.name for t in m.tools}
    declared_classes = set(m.permissions.knowledge.access_classes)
    for tool, scopes in p.invoke.items():
        if tool not in granted:
            problems.append(f"invoke.{tool}: the manifest does not grant this tool")
        tc = tool_contracts.get(tool)
        if tc is None:
            problems.append(f"invoke.{tool}: no such tool contract")
            continue
        for s in scopes:
            if s not in tc.scopes:
                problems.append(f"invoke.{tool}: {s!r} is not a scope of the tool")
            elif s not in tc.allowed_agents.get(p.agent, []):
                problems.append(f"invoke.{tool}: the tool's allowlist does not grant {s!r} to {p.agent}")
            cls = SCOPE_CLASS.get(s)
            if cls and cls not in p.read.knowledge_classes:
                problems.append(f"invoke.{tool}: scope {s!r} reaches {cls!r}, not in read.knowledge_classes")
        if tc.side_effects == "write" and tool not in p.write:
            problems.append(f"invoke.{tool}: the tool writes; list it in write")
    for c in p.read.knowledge_classes:
        if c not in declared_classes:
            problems.append(f"read: {c!r} is not in the manifest's knowledge access classes")
    for w in p.write:
        tc = tool_contracts.get(w)
        if tc is None or tc.side_effects != "write":
            problems.append(f"write: {w!r} is not a tool with side effects")
    for d in p.delegate:
        if d not in manifests:
            problems.append(f"delegate: {d!r} is not a registered agent")
        elif d in policies and not policies[d].accept_delegation:
            problems.append(f"delegate: {d!r} does not accept delegated work")
    if m.safety.serves_minors and not p.sensitive.redact_personal_data:
        problems.append("an agent serving minors must redact personal data")
    return problems


def load(directory: str = POLICY_DIR) -> dict[str, AgentPolicy]:
    out: dict[str, AgentPolicy] = {}
    for f in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if not f.endswith(".json"):
            continue
        path = os.path.join(directory, f)
        with open(path) as fh:
            raw = json.load(fh)
        try:
            p = AgentPolicy.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            errs = getattr(exc, "errors", None)
            raise PolicyError(path, [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errs()]
                              if errs else [str(exc)]) from None
        if f != f"{p.agent}.json":
            raise PolicyError(path, [f"file must be named {p.agent}.json"])
        out[p.agent] = p
    return out


def validate_all(policies: dict[str, AgentPolicy], manifests: dict, tool_contracts: dict,
                 path: Optional[str] = None) -> None:
    for p in policies.values():
        problems = check(p, manifests, tool_contracts, policies)
        if problems:
            raise PolicyError(os.path.join(path or POLICY_DIR, f"{p.agent}.json"), problems)


def json_schema() -> dict:
    s = AgentPolicy.model_json_schema()
    s["title"] = "NEXA agent policy (nexa.agent-policy/1.0)"
    s["x-nexa-scope-classes"] = SCOPE_CLASS
    return s
