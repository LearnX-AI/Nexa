"""Agent registry (NEXA-ADR-0003, NEXA-M3-001).

Agents are defined by manifests (nexa.agent/1.0) in v1/agent_runtime/agents/,
one JSON file per agent, validated at load: structure, then platform rules
(models, roles, permissions, tools, lifecycle). A manifest that fails stops the
load, so the runtime never serves an agent nobody validated.

AgentSpec is the runtime's view of a manifest: what the request path needs.
The full manifest stays available in MANIFESTS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import manifest as mf


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    model: str
    system_prompt: str
    required_permission: str = "model.invoke"
    version: str = "0.1.0"
    deprecated: bool = False
    deprecated_since: Optional[str] = None   # ISO date
    replaced_by: Optional[str] = None
    capabilities: tuple = ("chat",)
    tools: tuple = ()
    # From the manifest (NEXA-M3-001).
    state: str = "active"
    allowed_roles: tuple = ()                # empty: any role holding required_permission
    sunset_on: Optional[str] = None          # ISO date after which a deprecated agent stops running
    fallbacks: tuple = ()
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    timeout_s: float = 120.0
    max_attempts: int = 1
    backoff_s: float = 0.0
    retry_on: tuple = ()
    fingerprint: Optional[str] = None

    @property
    def runnable(self) -> bool:
        if self.state == "active":
            return True
        if self.state == "deprecated" or self.deprecated:
            return not self.past_sunset
        return False

    @property
    def past_sunset(self) -> bool:
        return bool(self.sunset_on) and date.today() > date.fromisoformat(self.sunset_on)


def spec_from_manifest(m: mf.AgentManifest) -> AgentSpec:
    lc = m.lifecycle
    return AgentSpec(
        name=m.id, description=m.identity.description, model=m.model.model, system_prompt=m.prompt.system,
        required_permission=m.permissions.run_permission, version=m.identity.version,
        deprecated=lc.state == mf.LifecycleState.deprecated,
        deprecated_since=lc.deprecated_on.isoformat() if lc.deprecated_on else None,
        replaced_by=lc.replaced_by, capabilities=tuple(c.value for c in m.capabilities),
        tools=tuple(t.name for t in m.tools), state=lc.state.value,
        allowed_roles=tuple(m.permissions.allowed_roles),
        sunset_on=lc.sunset_on.isoformat() if lc.sunset_on else None,
        fallbacks=tuple(m.model.fallbacks), temperature=m.model.temperature,
        max_output_tokens=m.model.max_output_tokens, timeout_s=m.runtime.timeout_s,
        max_attempts=m.runtime.retry.max_attempts, backoff_s=m.runtime.retry.backoff_s,
        retry_on=tuple(m.runtime.retry.retry_on), fingerprint=m.fingerprint(),
    )


MANIFESTS: dict[str, mf.AgentManifest] = {}
AGENTS: dict[str, AgentSpec] = {}


def reload(directory: Optional[str] = None) -> None:
    """Load (or reload) every manifest; replaces the registry in place."""
    found = mf.load_dir(directory or os.environ.get("NEXA_AGENT_MANIFEST_DIR") or mf.AGENTS_DIR)
    MANIFESTS.clear()
    MANIFESTS.update(found)
    AGENTS.clear()
    AGENTS.update({k: spec_from_manifest(v) for k, v in found.items()})


def get_agent(name: str) -> Optional[AgentSpec]:
    return AGENTS.get(name)


reload()
