"""Agent registry and discovery (NEXA-ADR-0003, NEXA-M3-001, NEXA-M3-002).

The authoritative record of NEXA agents: every registered version of every
agent (its nexa.agent/1.0 manifest, fingerprint and lifecycle state), whether
the agent is enabled, its capabilities, where each version is deployed and
how healthy that deployment is, and an audit trail of every change. Stored in
SQLite (store.py).

  register     validate a manifest (M3-001: structure and platform rules) and
               record it as a new version; versions are immutable
  set_state    move a version through the lifecycle (allowed transitions only;
               approval and deprecation details recorded)
  disable      the kill switch: an agent is refused at once, whatever its state
  query        agents by capability, audience, role, state
  discover     the agents a caller may use for a capability, with their
               healthy deployments: what callers route by, instead of a
               hard-coded table
  deployments  where each version runs (core NEXA, a school server), with
               heartbeats and health

The manifests in v1/agent_runtime/agents/ seed the registry at start: a new
file or version is registered, an unchanged one is left alone, and a changed
one without a new version number is refused (versions are immutable). After
seeding, the registry, not the file, is authoritative for each version's state.

AgentSpec is the runtime's view of one version; AGENTS holds the current
version of each agent for the request path and is rebuilt after every change.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from v1.identity.principal import Principal
from v1.identity.rbac import can
from v1.observability import audit_event

from . import manifest as mf
from .store import Store

HEARTBEAT_STALE_S = float(os.environ.get("NEXA_AGENT_HEARTBEAT_STALE_S", "120"))
SEED_ACTOR = "system:manifest-seed"


class RegistryError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code, self.detail = status_code, detail
        super().__init__(str(detail))


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
    # From the registry (NEXA-M3-002).
    enabled: bool = True
    disabled_reason: Optional[str] = None

    @property
    def runnable(self) -> bool:
        if not self.enabled:
            return False
        if self.state == "active":
            return True
        if self.state == "deprecated" or self.deprecated:
            return not self.past_sunset
        return False

    @property
    def past_sunset(self) -> bool:
        return bool(self.sunset_on) and date.today() > date.fromisoformat(self.sunset_on)


def spec_from_manifest(m: mf.AgentManifest, *, enabled: bool = True,
                       disabled_reason: Optional[str] = None) -> AgentSpec:
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
        enabled=enabled, disabled_reason=disabled_reason,
    )


def _semver(v: str) -> tuple[int, int, int, str]:
    core, _, pre = v.partition("-")
    a, b, c = (int(x) for x in core.split("."))
    return a, b, c, pre


def _newer(a: str, b: str) -> bool:
    """a > b in semantic order (a pre-release sorts before its release)."""
    x, y = _semver(a), _semver(b)
    if x[:3] != y[:3]:
        return x[:3] > y[:3]
    return (x[3] == "" and y[3] != "") or (x[3] != "" and y[3] != "" and x[3] > y[3])


# ---------------------------------------------------------------- the registry

class Registry:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------ reading

    def _manifest(self, row) -> mf.AgentManifest:
        """The manifest as registered, with the registry's lifecycle record."""
        raw = json.loads(row["manifest"])
        return mf.AgentManifest.model_validate(raw)

    def versions(self, agent_id: str) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT * FROM agent_versions WHERE agent_id = ? "
                                "ORDER BY major, minor, patch, version", (agent_id,))
        return [self._version_row(r) for r in rows]

    def _version_row(self, r) -> dict[str, Any]:
        m = self._manifest(r)
        return {"agent_id": r["agent_id"], "version": r["version"], "state": r["state"],
                "fingerprint": r["fingerprint"], "registered_by": r["registered_by"],
                "registered_at": r["registered_at"], "state_changed_by": r["state_changed_by"],
                "state_changed_at": r["state_changed_at"], "manifest_fingerprint": m.fingerprint(),
                "capabilities": [c.value for c in m.capabilities], "approved_by": m.lifecycle.approved_by,
                "approved_on": m.lifecycle.approved_on.isoformat() if m.lifecycle.approved_on else None,
                "sunset_on": m.lifecycle.sunset_on.isoformat() if m.lifecycle.sunset_on else None,
                "replaced_by": m.lifecycle.replaced_by}

    def manifest(self, agent_id: str, version: Optional[str] = None) -> Optional[mf.AgentManifest]:
        row = self._row(agent_id, version)
        return self._manifest(row) if row else None

    def _row(self, agent_id: str, version: Optional[str] = None):
        if version:
            rows = self.store.query("SELECT * FROM agent_versions WHERE agent_id = ? AND version = ?",
                                    (agent_id, version))
            return rows[0] if rows else None
        return self._current_row(agent_id)

    def _current_row(self, agent_id: str):
        """The highest runnable version; if none is runnable, the highest version
        (so a caller is told why it cannot run, not that it does not exist)."""
        rows = self.store.query("SELECT * FROM agent_versions WHERE agent_id = ?", (agent_id,))
        if not rows:
            return None
        rank = sorted(rows, key=lambda r: _semver(r["version"])[:3], reverse=True)
        runnable = [r for r in rank if r["state"] in ("active", "deprecated")
                    and not self._past_sunset(r)]
        return (runnable or rank)[0]

    @staticmethod
    def _past_sunset(r) -> bool:
        s = (json.loads(r["manifest"]).get("lifecycle") or {}).get("sunset_on")
        return bool(s) and date.today() > date.fromisoformat(s)

    def agent_row(self, agent_id: str):
        rows = self.store.query("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
        return rows[0] if rows else None

    def spec(self, agent_id: str, version: Optional[str] = None) -> Optional[AgentSpec]:
        row, a = self._row(agent_id, version), self.agent_row(agent_id)
        if row is None or a is None:
            return None
        return spec_from_manifest(self._manifest(row), enabled=bool(a["enabled"]),
                                  disabled_reason=a["disabled_reason"])

    def agent_ids(self) -> list[str]:
        return [r["agent_id"] for r in self.store.query("SELECT agent_id FROM agents ORDER BY agent_id")]

    def summary(self, agent_id: str) -> Optional[dict[str, Any]]:
        spec, a = self.spec(agent_id), self.agent_row(agent_id)
        if spec is None:
            return None
        m = self.manifest(agent_id, spec.version)
        return {"agent_id": agent_id, "name": m.identity.name, "version": spec.version, "state": spec.state,
                "enabled": bool(a["enabled"]), "disabled_reason": a["disabled_reason"],
                "runnable": spec.runnable, "description": spec.description,
                "capabilities": list(spec.capabilities), "audience": [x.value for x in m.purpose.audience],
                "required_permission": spec.required_permission, "allowed_roles": list(spec.allowed_roles),
                "model": spec.model, "fallbacks": list(spec.fallbacks), "tools": list(spec.tools),
                "depends_on": list(m.health.requires), "fingerprint": spec.fingerprint,
                "versions": [v["version"] for v in self.versions(agent_id)],
                "deployments": self.deployments(agent_id, spec.version)}

    def query(self, *, capability: Optional[str] = None, audience: Optional[str] = None,
              state: Optional[str] = None, runnable_only: bool = False) -> list[dict[str, Any]]:
        ids = self.agent_ids()
        if capability:
            have = {r["agent_id"] for r in self.store.query(
                "SELECT DISTINCT agent_id FROM capabilities WHERE capability = ?", (capability,))}
            ids = [i for i in ids if i in have]
        out = []
        for i in ids:
            s = self.summary(i)
            if s is None:
                continue
            if capability and capability not in s["capabilities"]:
                continue          # an older version had it; the current one does not
            if audience and audience not in s["audience"]:
                continue
            if state and s["state"] != state:
                continue
            if runnable_only and not s["runnable"]:
                continue
            out.append(s)
        return out

    def discover(self, principal: Principal, *, capability: Optional[str] = None,
                 location: Optional[str] = None) -> list[dict[str, Any]]:
        """Agents this caller may run now for a capability, each with its healthy
        deployments (the caller's location first). Not runnable, disabled, or
        not permitted for this caller: left out."""
        out = []
        for s in self.query(capability=capability, runnable_only=True):
            allowed = (not s["allowed_roles"] or bool(set(principal.roles) & set(s["allowed_roles"])))
            if not (allowed and can(principal, s["required_permission"])):
                continue
            deps = [d for d in s["deployments"] if d["healthy"]]
            deps.sort(key=lambda d: (d["location"] != location, -d["last_heartbeat"]))
            if not deps:
                continue
            out.append({k: s[k] for k in ("agent_id", "name", "version", "description", "capabilities",
                                          "audience", "fingerprint")} | {"deployments": deps})
        return out

    def resolve(self, agent_id: str, *, location: Optional[str] = None,
                version: Optional[str] = None) -> Optional[dict[str, Any]]:
        """The deployment a request for this agent should go to."""
        spec = self.spec(agent_id, version)
        if spec is None:
            return None
        deps = [d for d in self.deployments(agent_id, spec.version) if d["healthy"]]
        deps.sort(key=lambda d: (d["location"] != location, -d["last_heartbeat"]))
        return {"agent_id": agent_id, "version": spec.version, "state": spec.state, "enabled": spec.enabled,
                "runnable": spec.runnable, "deployment": deps[0] if deps else None}

    def history(self, agent_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        if agent_id:
            rows = self.store.query("SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                                    (agent_id, limit))
        else:
            rows = self.store.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                 "agent_id": r["agent_id"], "version": r["version"], "detail": json.loads(r["detail"])}
                for r in rows]

    # ------------------------------------------------------------ registering

    def register(self, raw: dict[str, Any], actor: str, *,
                 also_known: Optional[dict[str, mf.AgentManifest]] = None) -> dict[str, Any]:
        """Validate and record a manifest. Returns {"result": "registered" |
        "unchanged", ...}. Raises RegistryError: 422 invalid, 409 a different
        manifest under an existing version, or a version not newer than the latest."""
        try:
            m = mf.AgentManifest.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError, reported as a list
            errors = getattr(exc, "errors", None)
            raise RegistryError(422, [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errors()]
                                if errors else [str(exc)]) from None
        others = {i: self.manifest(i) for i in self.agent_ids()} | (also_known or {})
        others[m.id] = m
        problems = mf.validate_platform(m, others=others)
        if problems:
            raise RegistryError(422, problems)
        fp = m.content_fingerprint()
        existing = self._row(m.id, m.identity.version)
        if existing is not None:
            if existing["fingerprint"] == fp:
                return {"result": "unchanged", "agent_id": m.id, "version": m.identity.version,
                        "state": existing["state"], "fingerprint": fp}
            raise RegistryError(409, f"{m.id} {m.identity.version} is already registered with different content "
                                     f"(versions are immutable): raise the version")
        latest = [v["version"] for v in self.versions(m.id)]
        if latest and not _newer(m.identity.version, max(latest, key=lambda v: _semver(v)[:3])):
            raise RegistryError(409, f"{m.id} {m.identity.version} is not newer than the latest registered "
                                     f"version {max(latest, key=lambda v: _semver(v)[:3])}")
        a, b, c, _ = _semver(m.identity.version)
        now = time.time()
        with self.store.tx() as db:
            if self.agent_row(m.id) is None:
                db.execute("INSERT INTO agents (agent_id, enabled, created_at) VALUES (?, 1, ?)", (m.id, now))
                Store.event(db, actor, "agent.created", m.id, None, name=m.identity.name, owner=m.identity.owner)
            db.execute("INSERT INTO agent_versions (agent_id, version, major, minor, patch, state, fingerprint, "
                       "manifest, registered_by, registered_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (m.id, m.identity.version, a, b, c, m.lifecycle.state.value, fp,
                        json.dumps(m.model_dump(mode="json"), sort_keys=True), actor, now))
            for cap in m.capabilities:
                db.execute("INSERT INTO capabilities VALUES (?,?,?)", (m.id, m.identity.version, cap.value))
            Store.event(db, actor, "version.registered", m.id, m.identity.version, state=m.lifecycle.state.value,
                        fingerprint=fp, capabilities=[c.value for c in m.capabilities])
        audit_event(action=f"agent.registry.register:{m.id}", decision="allow", principal=actor,
                    version=m.identity.version)
        _refresh()
        return {"result": "registered", "agent_id": m.id, "version": m.identity.version,
                "state": m.lifecycle.state.value, "fingerprint": fp}

    def set_state(self, agent_id: str, version: str, new_state: str, actor: str, *,
                  approved_by: Optional[str] = None, approved_on: Optional[str] = None,
                  sunset_on: Optional[str] = None, replaced_by: Optional[str] = None,
                  reason: str = "") -> dict[str, Any]:
        row = self._row(agent_id, version)
        if row is None:
            raise RegistryError(404, f"Unknown agent version: {agent_id} {version}")
        try:
            old, new = mf.LifecycleState(row["state"]), mf.LifecycleState(new_state)
        except ValueError:
            raise RegistryError(422, f"Unknown lifecycle state: {new_state}") from None
        if not mf.can_transition(old, new):
            allowed = ", ".join(sorted(s.value for s in mf.TRANSITIONS[old])) or "none"
            raise RegistryError(409, f"{agent_id} {version}: {old.value} cannot become {new.value} "
                                     f"(allowed: {allowed})")
        raw = json.loads(row["manifest"])
        lc = raw["lifecycle"]
        lc["state"] = new.value
        today = date.today().isoformat()
        if new == mf.LifecycleState.approved:
            if actor == row["registered_by"]:
                raise RegistryError(403, "a version cannot be approved by the identity that registered it")
            if not approved_by:
                raise RegistryError(422, "approval needs approved_by (the approver's name)")
            lc["approved_by"], lc["approved_on"] = approved_by, approved_on or today
        if new == mf.LifecycleState.deprecated:
            if not sunset_on:
                raise RegistryError(422, "deprecation needs sunset_on (the date it stops running)")
            lc["deprecated_on"], lc["sunset_on"] = today, sunset_on
            if replaced_by:
                lc["replaced_by"] = replaced_by
        if new == mf.LifecycleState.active and old == mf.LifecycleState.deprecated:
            lc["deprecated_on"] = lc["sunset_on"] = None
        if new == mf.LifecycleState.draft:
            lc["approved_by"] = lc["approved_on"] = None
        try:
            m = mf.AgentManifest.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            raise RegistryError(422, str(exc)) from None
        others = {i: self.manifest(i) for i in self.agent_ids()}
        problems = mf.validate_platform(m, others=others)
        if problems:
            # e.g. activating an agent whose tools are not built yet
            raise RegistryError(422, problems)
        with self.store.tx() as db:
            db.execute("UPDATE agent_versions SET state = ?, manifest = ?, state_changed_by = ?, "
                       "state_changed_at = ? WHERE agent_id = ? AND version = ?",
                       (new.value, json.dumps(m.model_dump(mode="json"), sort_keys=True), actor, time.time(),
                        agent_id, version))
            Store.event(db, actor, "version.state", agent_id, version, old=old.value, new=new.value,
                        reason=reason, approved_by=lc.get("approved_by"), sunset_on=lc.get("sunset_on"),
                        replaced_by=lc.get("replaced_by"))
        audit_event(action=f"agent.registry.state:{agent_id}", decision="allow", principal=actor,
                    version=version, old=old.value, new=new.value)
        _refresh()
        return {"agent_id": agent_id, "version": version, "old": old.value, "state": new.value}

    def set_enabled(self, agent_id: str, enabled: bool, actor: str, reason: str = "") -> dict[str, Any]:
        a = self.agent_row(agent_id)
        if a is None:
            raise RegistryError(404, f"Unknown agent: {agent_id}")
        if not enabled and not reason:
            raise RegistryError(422, "disabling an agent needs a reason")
        if bool(a["enabled"]) == enabled:
            return {"agent_id": agent_id, "enabled": enabled, "result": "unchanged"}
        with self.store.tx() as db:
            if enabled:
                db.execute("UPDATE agents SET enabled = 1, disabled_reason = NULL, disabled_by = NULL, "
                           "disabled_at = NULL WHERE agent_id = ?", (agent_id,))
            else:
                db.execute("UPDATE agents SET enabled = 0, disabled_reason = ?, disabled_by = ?, disabled_at = ? "
                           "WHERE agent_id = ?", (reason, actor, time.time(), agent_id))
            Store.event(db, actor, "agent.enabled" if enabled else "agent.disabled", agent_id, None, reason=reason)
        audit_event(action=f"agent.registry.{'enable' if enabled else 'disable'}:{agent_id}", decision="allow",
                    principal=actor, reason=reason)
        _refresh()
        return {"agent_id": agent_id, "enabled": enabled, "result": "changed"}

    # ------------------------------------------------------------ deployments

    def register_deployment(self, agent_id: str, version: str, *, location: str, endpoint: str, instance: str,
                            actor: str) -> dict[str, Any]:
        if self._row(agent_id, version) is None:
            raise RegistryError(404, f"Unknown agent version: {agent_id} {version}")
        dep_id = f"{instance}/{agent_id}@{version}"
        now = time.time()
        known = self.store.query("SELECT location, endpoint FROM deployments WHERE deployment_id = ?", (dep_id,))
        with self.store.tx() as db:
            db.execute("INSERT INTO deployments (deployment_id, agent_id, version, location, endpoint, instance, "
                       "registered_by, registered_at, last_heartbeat, health) VALUES (?,?,?,?,?,?,?,?,?,'{}') "
                       "ON CONFLICT(deployment_id) DO UPDATE SET location = excluded.location, "
                       "endpoint = excluded.endpoint, last_heartbeat = excluded.last_heartbeat",
                       (dep_id, agent_id, version, location, endpoint, instance, actor, now, now))
            if not known or (known[0]["location"], known[0]["endpoint"]) != (location, endpoint):
                Store.event(db, actor, "deployment.registered", agent_id, version, deployment_id=dep_id,
                            location=location, endpoint=endpoint, instance=instance)
        return {"deployment_id": dep_id}

    def heartbeat(self, deployment_id: str, health: dict[str, Any], actor: str) -> None:
        rows = self.store.query("SELECT agent_id, version, health FROM deployments WHERE deployment_id = ?",
                                (deployment_id,))
        if not rows:
            raise RegistryError(404, f"Unknown deployment: {deployment_id}")
        before = json.loads(rows[0]["health"] or "{}").get("ready")
        with self.store.tx() as db:
            db.execute("UPDATE deployments SET last_heartbeat = ?, health = ? WHERE deployment_id = ?",
                       (time.time(), json.dumps(health, sort_keys=True), deployment_id))
            if before is not None and before != health.get("ready"):   # only changes are audited, not every beat
                Store.event(db, actor, "deployment.health", rows[0]["agent_id"], rows[0]["version"],
                            deployment_id=deployment_id, ready=health.get("ready"), checks=health.get("checks"))

    def deregister_deployment(self, deployment_id: str, actor: str) -> None:
        rows = self.store.query("SELECT agent_id, version FROM deployments WHERE deployment_id = ?",
                                (deployment_id,))
        if not rows:
            return
        with self.store.tx() as db:
            db.execute("DELETE FROM deployments WHERE deployment_id = ?", (deployment_id,))
            Store.event(db, actor, "deployment.removed", rows[0]["agent_id"], rows[0]["version"],
                        deployment_id=deployment_id)

    def deployments(self, agent_id: str, version: Optional[str] = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM deployments WHERE agent_id = ?", [agent_id]
        if version:
            sql += " AND version = ?"
            args.append(version)
        now = time.time()
        out = []
        for r in self.store.query(sql, args):
            h = json.loads(r["health"] or "{}")
            fresh = now - r["last_heartbeat"] <= HEARTBEAT_STALE_S
            out.append({"deployment_id": r["deployment_id"], "version": r["version"], "location": r["location"],
                        "endpoint": r["endpoint"], "instance": r["instance"], "last_heartbeat": r["last_heartbeat"],
                        "fresh": fresh, "ready": bool(h.get("ready")), "checks": h.get("checks", {}),
                        "healthy": fresh and bool(h.get("ready"))})
        return out

    # ------------------------------------------------------------ seeding

    def seed(self, directory: str = mf.AGENTS_DIR) -> list[dict[str, Any]]:
        """Register every manifest file not yet in the registry. Fails loudly on
        an invalid manifest or a changed manifest that kept its version."""
        manifests = mf.load_dir(directory)
        results = []
        for m in sorted(manifests.values(), key=lambda x: x.id):
            results.append(self.register(m.model_dump(mode="json"), SEED_ACTOR, also_known=manifests))
        return results


# ---------------------------------------------------------------- module state

REGISTRY: Optional[Registry] = None
AGENTS: dict[str, AgentSpec] = {}           # current version of each agent, for the request path
MANIFESTS: dict[str, mf.AgentManifest] = {}  # its manifest


def _refresh() -> None:
    if REGISTRY is None:
        return
    AGENTS.clear()
    MANIFESTS.clear()
    for i in REGISTRY.agent_ids():
        spec = REGISTRY.spec(i)
        if spec is not None:
            AGENTS[i] = spec
            MANIFESTS[i] = REGISTRY.manifest(i, spec.version)


def reload(path: Optional[str] = None, directory: Optional[str] = None) -> Registry:
    """Open the registry (path: its database; ":memory:" for a throwaway one),
    seed it from the manifest files, and rebuild the request-path view."""
    global REGISTRY
    if REGISTRY is not None:
        REGISTRY.store.close()
    REGISTRY = Registry(Store(path))
    REGISTRY.seed(directory or os.environ.get("NEXA_AGENT_MANIFEST_DIR") or mf.AGENTS_DIR)
    _refresh()
    # The agent policies (NEXA-M3-007) are checked against these manifests.
    from .policy import pdp
    pdp.load(os.environ.get("NEXA_AGENT_POLICY_DIR"))
    return REGISTRY


def get_agent(name: str, version: Optional[str] = None) -> Optional[AgentSpec]:
    if version and REGISTRY is not None:
        return REGISTRY.spec(name, version)
    return AGENTS.get(name)


reload()
