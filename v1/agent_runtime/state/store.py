"""Session state, run checkpoints and the state access log (NEXA-M3-006).

Kept in the registry database (store.py): sessions, run_checkpoints,
state_access. Every read and write of state, allowed or refused, is one row
in state_access and one audit event.

Session policy
  values      only the owner reads or writes them, directly or through a run
              of a workflow the owner named when creating the session, and
              then only the keys that workflow declares
  metadata    the owner, admin and platform_admin (id, owner, workflows,
              version, timestamps, size; never the values)
  delete      the owner, admin and platform_admin (for example, on request)
  expiry      sliding: idle for ttl_s, then expired; never older than the
              maximum age; expired sessions are refused and purged

Run checkpoints
  written after every step by the orchestration engine, each with a version
  and a digest; the last few are kept while the run lives; a resume uses the
  newest whose digest matches its data
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from v1.identity.principal import Principal
from v1.observability import audit_event

from . import contract as sc

KEEP_CHECKPOINTS = 5
META_ROLES = {"admin", "platform_admin"}
ACCESS_ROLES = {"auditor", "admin", "platform_admin", "ai_engineer"}


def _store():
    from .. import registry
    if registry.REGISTRY is None:
        raise sc.StateError(503, "agent registry not open")
    return registry.REGISTRY.store


def log_access(actor: Principal, action: str, kind: str, target: str, *, decision: str = "allow",
               version: Optional[int] = None, keys: Optional[list[str]] = None, reason: Optional[str] = None,
               via: Optional[str] = None, trace_id: Optional[str] = None) -> None:
    if reason is not None and not isinstance(reason, str):
        reason = json.dumps(reason, ensure_ascii=False)[:1000]
    with _store().tx() as db:
        db.execute("INSERT INTO state_access (ts, actor, roles, action, kind, target, version, keys, via, decision, "
                   "reason, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time(), actor.subject, json.dumps(list(actor.roles)), action, kind, target, version,
                    json.dumps(sorted(keys)) if keys is not None else None, via, decision, reason, trace_id))
    audit_event(action=f"state.{kind}.{action}", decision=decision, principal=actor.subject, target=target,
                version=version, keys=sorted(keys) if keys is not None else None, via=via, reason=reason,
                trace_id=trace_id)


def access_log(target: Optional[str] = None, actor: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM state_access", []
    where = []
    if target:
        where.append("target = ?")
        args.append(target)
    if actor:
        where.append("actor = ?")
        args.append(actor)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in _store().query(sql, args):
        d = dict(r)
        d["roles"] = json.loads(d["roles"] or "[]")
        d["keys"] = json.loads(d["keys"]) if d["keys"] else None
        out.append(d)
    return out


# ---------------------------------------------------------------- sessions

def _row_to_record(r) -> sc.StateRecord:
    return sc.StateRecord(kind="session", id=r["session_id"],
                          owner=sc.Owner(subject=r["owner"], roles=json.loads(r["owner_roles"])),
                          scope={"workflows": json.loads(r["workflows"]), "ttl_s": r["ttl_s"],
                                 "max_expires_at": r["max_expires_at"]},
                          version=r["version"], created_at=r["created_at"], updated_at=r["updated_at"],
                          expires_at=r["expires_at"], data=json.loads(r["data"]), digest=r["digest"])


def create_session(principal: Principal, workflows: list[str], ttl_s: int = sc.SESSION_TTL_DEFAULT,
                   *, known_workflows: set[str], trace_id: Optional[str] = None) -> sc.StateRecord:
    unknown = sorted(set(workflows) - known_workflows)
    if not workflows or unknown:
        log_access(principal, "create", "session", "(new)", decision="deny",
                   reason=f"unknown workflows {unknown}" if unknown else "no workflows", trace_id=trace_id)
        raise sc.StateError(422, f"name the workflows that may use the session (unknown: {unknown})")
    if not 60 <= ttl_s <= sc.SESSION_TTL_MAX:
        raise sc.StateError(422, f"ttl_s must be between 60 and {sc.SESSION_TTL_MAX}")
    now = time.time()
    sid = "ses_" + uuid.uuid4().hex[:24]
    data: dict[str, Any] = {}
    with _store().tx() as db:
        db.execute("INSERT INTO sessions (session_id, owner, owner_roles, workflows, version, created_at, updated_at, "
                   "expires_at, max_expires_at, ttl_s, data, digest) VALUES (?,?,?,?,1,?,?,?,?,?,?,?)",
                   (sid, principal.subject, json.dumps(list(principal.roles)), json.dumps(sorted(set(workflows))),
                    now, now, now + ttl_s, now + sc.SESSION_MAX_AGE, ttl_s, json.dumps(data), sc.digest(data)))
    log_access(principal, "create", "session", sid, version=1, keys=[], trace_id=trace_id)
    return load_session_raw(sid)


def load_session_raw(sid: str) -> Optional[sc.StateRecord]:
    rows = _store().query("SELECT * FROM sessions WHERE session_id = ?", (sid,))
    return _row_to_record(rows[0]) if rows else None


def _authorize(sid: str, principal: Principal, action: str, *, values: bool, via: Optional[str],
               keys: Optional[list[str]], trace_id: Optional[str]) -> sc.StateRecord:
    rec = load_session_raw(sid)

    def deny(code, reason):
        log_access(principal, action, "session", sid, decision="deny", reason=reason, via=via, keys=keys,
                   trace_id=trace_id)
        return sc.StateError(code, reason)
    if rec is None:
        raise deny(404, f"Unknown session: {sid}")
    now = time.time()
    if now > (rec.expires_at or 0) or now > rec.scope["max_expires_at"]:
        delete_session_row(sid)
        raise deny(410, "the session has expired")
    owner = principal.subject == rec.owner.subject
    if values and not owner:
        raise deny(404 if not set(principal.roles) & META_ROLES else 403,
                   "only the session's owner can use its state")
    if not values and not (owner or set(principal.roles) & META_ROLES):
        raise deny(404, f"Unknown session: {sid}")
    if via is not None and via not in rec.scope["workflows"]:
        raise deny(403, f"workflow {via} may not use this session")
    if digest_problem(rec):
        raise deny(409, "the session record does not match its digest; it will not be used")
    return rec


def digest_problem(rec: sc.StateRecord) -> bool:
    return sc.digest(rec.data) != rec.digest


def read_session(sid: str, principal: Principal, *, keys: Optional[list[str]] = None, via: Optional[str] = None,
                 trace_id: Optional[str] = None) -> sc.StateRecord:
    rec = _authorize(sid, principal, "read", values=True, via=via, keys=keys, trace_id=trace_id)
    data = rec.data if keys is None else {k: rec.data[k] for k in keys if k in rec.data}
    log_access(principal, "read", "session", sid, version=rec.version, keys=list(data), via=via, trace_id=trace_id)
    return rec.model_copy(update={"data": data})


def session_metadata(sid: str, principal: Principal, trace_id: Optional[str] = None) -> dict[str, Any]:
    rec = _authorize(sid, principal, "metadata", values=False, via=None, keys=None, trace_id=trace_id)
    log_access(principal, "metadata", "session", sid, version=rec.version, trace_id=trace_id)
    return {"session_id": rec.id, "owner": rec.owner.subject, "workflows": rec.scope["workflows"],
            "version": rec.version, "created_at": rec.created_at, "updated_at": rec.updated_at,
            "expires_at": rec.expires_at, "max_expires_at": rec.scope["max_expires_at"], "ttl_s": rec.scope["ttl_s"],
            "keys": sorted(rec.data), "bytes": len(json.dumps(rec.data)), "digest": rec.digest}


def write_session(sid: str, principal: Principal, updates: dict[str, Any], *, expected_version: int,
                  via: Optional[str] = None, allowed_keys: Optional[set[str]] = None,
                  trace_id: Optional[str] = None) -> sc.StateRecord:
    keys = list(updates)
    rec = _authorize(sid, principal, "write", values=True, via=via, keys=keys, trace_id=trace_id)

    def deny(code, reason):
        log_access(principal, "write", "session", sid, decision="deny", reason=reason, via=via, keys=keys,
                   version=rec.version, trace_id=trace_id)
        return sc.StateError(code, reason)
    bad = [k for k in keys if not sc.SESSION_KEY.match(k)]
    if bad:
        raise deny(422, f"session keys are lower snake case: {bad}")
    if allowed_keys is not None and set(keys) - allowed_keys:
        raise deny(403, f"keys not declared by {via}: {sorted(set(keys) - allowed_keys)}")
    if expected_version != rec.version:
        raise deny(409, f"version conflict: expected {expected_version}, the session is at {rec.version}")
    data = {**rec.data, **{k: v for k, v in updates.items() if v is not None}}
    for k, v in updates.items():
        if v is None:
            data.pop(k, None)
    problems = sc.check_data(data, "session")
    if problems:
        raise deny(422, problems)
    now = time.time()
    with _store().tx() as db:
        changed = db.execute("UPDATE sessions SET data = ?, digest = ?, version = version + 1, updated_at = ?, "
                             "expires_at = MIN(?, max_expires_at) WHERE session_id = ? AND version = ?",
                             (json.dumps(data, ensure_ascii=False), sc.digest(data), now, now + rec.scope["ttl_s"],
                              sid, rec.version)).rowcount
    if not changed:
        raise deny(409, "version conflict: the session changed while writing")
    log_access(principal, "write", "session", sid, version=rec.version + 1, keys=keys, via=via, trace_id=trace_id)
    return load_session_raw(sid)


def delete_session(sid: str, principal: Principal, reason: str = "", trace_id: Optional[str] = None) -> None:
    rec = _authorize(sid, principal, "delete", values=False, via=None, keys=None, trace_id=trace_id)
    delete_session_row(sid)
    log_access(principal, "delete", "session", sid, version=rec.version, reason=reason or None, trace_id=trace_id)


def delete_session_row(sid: str) -> None:
    with _store().tx() as db:
        db.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))


# ---------------------------------------------------------------- run checkpoints

def write_checkpoint(run_id: str, owner: Principal, scope: dict[str, Any], data: dict[str, Any],
                     step: Optional[str], status: str) -> sc.StateRecord:
    problems = sc.check_data(data, "run_checkpoint")
    if problems:
        raise sc.StateError(422, problems)
    now = time.time()
    dig = sc.digest(data)
    with _store().tx() as db:
        version = db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM run_checkpoints WHERE run_id = ?",
                             (run_id,)).fetchone()[0]
        db.execute("INSERT INTO run_checkpoints (run_id, version, step, status, scope, owner, created_at, data, digest) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, version, step, status, json.dumps(scope), owner.subject, now,
                    json.dumps(data, ensure_ascii=False), dig))
        db.execute("DELETE FROM run_checkpoints WHERE run_id = ? AND version <= ?", (run_id, version - KEEP_CHECKPOINTS))
    return sc.StateRecord(kind="run_checkpoint", id=run_id, owner=sc.Owner(subject=owner.subject,
                                                                          roles=list(owner.roles)),
                          scope=scope | {"step": step, "status": status}, version=version, created_at=now,
                          updated_at=now, data=data, digest=dig)


def checkpoints(run_id: str) -> list[dict[str, Any]]:
    return [{"version": r["version"], "step": r["step"], "status": r["status"], "created_at": r["created_at"],
             "digest": r["digest"], "valid": sc.digest(json.loads(r["data"])) == r["digest"],
             "bytes": len(r["data"])}
            for r in _store().query("SELECT * FROM run_checkpoints WHERE run_id = ? ORDER BY version", (run_id,))]


def latest_valid_checkpoint(run_id: str) -> Optional[tuple[int, dict[str, Any], dict[str, Any]]]:
    """(version, scope, data) of the newest checkpoint whose digest matches."""
    for r in _store().query("SELECT * FROM run_checkpoints WHERE run_id = ? ORDER BY version DESC", (run_id,)):
        data = json.loads(r["data"])
        if sc.digest(data) == r["digest"]:
            return r["version"], json.loads(r["scope"]), data
    return None
