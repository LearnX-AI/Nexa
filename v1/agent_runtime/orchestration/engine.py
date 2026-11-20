"""The orchestration engine (NEXA-M3-003, under NEXA-ADR-0003).

Runs a reviewed workflow (workflow.py) for one user. The engine owns control:
it decides which step runs next, calls agents through the Agent Runtime
(registry, lifecycle, permissions and the kill switch all apply to every
call), enforces each step's timeout, retries and fallbacks, and the run's
budget. Agents only return text. An agent never calls another agent: the
runtime refuses a run started from inside another run (runtime.active_run), so
multi-agent work exists only as a delegate step, where the engine checks every
choice a coordinating agent makes against a fixed allowlist and a fixed limit.

Every run is persisted with its full trace (store.py: workflow_runs,
workflow_trace): each step, each routing decision and its reason, each agent
call (agent, version, manifest fingerprint, attempts, latency, trace id), each
coordinator choice as returned and as accepted, each human decision.

Run statuses
  running            steps are executing
  waiting_approval   paused at a checkpoint until a person decides
  succeeded          reached the end with an output
  rejected           a reviewer rejected it and the workflow ends there
  failed             a step failed and the workflow has no error path
  budget_exceeded    steps, agent calls or running time ran out
  expired            nobody decided before the checkpoint expired
  interrupted        the runtime restarted while the run was executing
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

import httpx

from v1.identity.principal import Principal
from v1.identity.rbac import ROLE_PERMISSIONS, can
from v1.observability import audit_event, new_trace_id

from .. import presence, registry, runtime, tools
from ..messaging import bus, contract as mc
from ..state import contract as sc, store as state
from ..tracing import signals as obs
from ..policy import pdp
from . import workflow as wfm

log = logging.getLogger("nexa.agent_runtime.orchestration")

# The orchestrator's identity in every message it sends (NEXA-M3-005).
ORCHESTRATOR = mc.Party(kind="orchestrator", id="nexa-orchestrator", version="1.0.0")

TERMINAL = {"succeeded", "rejected", "failed", "budget_exceeded", "expired", "interrupted"}
OVERSIGHT_ROLES = {"admin", "platform_admin", "national_admin", "auditor", "ai_engineer"}   # see runs of any school
MAX_TEXT = {"text": 4000, "short_text": 200}
CARRY = 1500      # characters of earlier delegate results shown to the coordinator


class OrchestrationError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code, self.detail = status_code, detail
        super().__init__(str(detail))


class StepError(Exception):
    """A step attempt failed. retry: whether another attempt may help."""
    def __init__(self, message: str, retry: bool = True, status: Optional[int] = None):
        self.retry, self.status = retry, status
        super().__init__(message)


class BudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------- workflows

WORKFLOWS: dict[str, wfm.Workflow] = {}


def load_workflows(directory: Optional[str] = None) -> dict[str, wfm.Workflow]:
    found = wfm.load_dir(directory or os.environ.get("NEXA_WORKFLOW_DIR") or wfm.WORKFLOWS_DIR,
                         agent_ids=set(registry.AGENTS), roles=ROLE_PERMISSIONS,
                         grants={a: set(s.tools) for a, s in registry.AGENTS.items()})
    # A workflow may not ask of an agent what its policy does not allow
    # (NEXA-M3-007): checked when workflows load, and again at each call.
    if pdp.POLICIES:
        for wf in found.values():
            problems = policy_problems(wf)
            if problems:
                raise ValueError(f"workflow {wf.id}: " + "; ".join(problems))
    WORKFLOWS.clear()
    WORKFLOWS.update(found)
    return found


def policy_problems(wf: wfm.Workflow) -> list[str]:
    out = []
    for s in wf.steps:
        if isinstance(s, wfm.DelegateStep):
            p = pdp.POLICIES.get(s.coordinator)
            for name, a in s.delegates.items():
                if p is None or a not in p.delegate:
                    out.append(f"step {s.id}: {s.coordinator}'s policy does not let it delegate to {a}")
                elif not (pdp.POLICIES.get(a) and pdp.POLICIES[a].accept_delegation):
                    out.append(f"step {s.id}: {a} does not accept delegated work")
        if isinstance(s, wfm.ToolStep):
            p = pdp.POLICIES.get(s.agent)
            if p is None or s.tool not in p.invoke:
                out.append(f"step {s.id}: {s.agent}'s policy does not let it invoke {s.tool}")
    return out


# ---------------------------------------------------------------- templates

def lookup(path: str, ctx: dict[str, Any]) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def render(template: str, ctx: dict[str, Any]) -> str:
    """Plain substitution; nothing is evaluated."""
    def sub(m: re.Match) -> str:
        v = lookup(m.group(1), ctx)
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return wfm.TEMPLATE.sub(sub, template)


def holds(c: wfm.Condition, ctx: dict[str, Any]) -> bool:
    v = lookup(c.field, ctx)
    if c.exists is not None:
        return (v not in (None, "", [], {})) == c.exists
    if c.equals is not None:
        return v == c.equals
    if c.one_of is not None:
        return v in c.one_of
    return v is not None and re.search(c.matches, str(v), re.IGNORECASE) is not None


# ---------------------------------------------------------------- persistence

def _store():
    if registry.REGISTRY is None:
        raise OrchestrationError(503, "agent registry not open")
    return registry.REGISTRY.store


class Run:
    """One workflow run in memory; saved after every step."""

    def __init__(self, row: dict[str, Any]):
        self.run_id: str = row["run_id"]
        self.wf: wfm.Workflow = WORKFLOWS.get(row["workflow_id"])
        self.workflow_id: str = row["workflow_id"]
        self.workflow_version: str = row["workflow_version"]
        self.status: str = row["status"]
        _ctx = json.loads(row["context"] or "{}")
        _tenant = _ctx.get("_tenant") or {}
        # The initiator with their tenant context (NEXA-M3-007): school and classes.
        self.principal = Principal(row["initiator"], tuple(json.loads(row["initiator_roles"])), "user",
                                   school_id=_tenant.get("school_id"),
                                   attrs={"class_ids": _tenant["class_ids"]} if _tenant.get("class_ids") else {})
        self.trace_id: str = row["trace_id"]
        self.input: dict[str, Any] = json.loads(row["input"])
        self.ctx: dict[str, Any] = json.loads(row["context"] or "{}")
        self.ctx.setdefault("steps", {})
        self.current: Optional[str] = row["current_step"]
        self.output: Optional[str] = row["output"]
        self.error: Optional[str] = row["error"]
        self.counters: dict[str, Any] = json.loads(row["counters"] or "{}")
        for k in ("steps", "agent_calls"):
            self.counters.setdefault(k, 0)
        self.counters.setdefault("running_s", 0.0)
        self.waiting_until: Optional[float] = row["waiting_until"]
        self.created_at: float = row["created_at"]
        self.auth: Optional[str] = None    # the initiator's bearer token: memory only, never stored
        self.attempts: dict[str, int] = {}  # idempotency key -> attempts made (NEXA-M3-005)

    @property
    def last_message(self) -> Optional[str]:
        return self.ctx.get("_last_message")

    @last_message.setter
    def last_message(self, message_id: str) -> None:
        self.ctx["_last_message"] = message_id

    @property
    def on_behalf_of(self) -> mc.OnBehalfOf:
        return mc.OnBehalfOf(subject=self.principal.subject, roles=list(self.principal.roles))

    @property
    def template_ctx(self) -> dict[str, Any]:
        return {"input": self.input, **self.ctx}

    def save(self) -> None:
        with _store().tx() as db:
            db.execute("UPDATE workflow_runs SET status = ?, context = ?, current_step = ?, output = ?, error = ?, "
                       "counters = ?, waiting_until = ?, updated_at = ? WHERE run_id = ?",
                       (self.status, json.dumps(self.ctx, ensure_ascii=False), self.current, self.output,
                        self.error, json.dumps(self.counters), self.waiting_until, time.time(), self.run_id))
        # A versioned, digested checkpoint of the run's state (NEXA-M3-006):
        # what a resume starts from. Never holds the user's token.
        rec = state.write_checkpoint(
            self.run_id, self.principal,
            {"workflow": self.workflow_id, "workflow_version": self.workflow_version,
             "fingerprint": self.wf.fingerprint() if self.wf else None},
            {"ctx": self.ctx, "current": self.current, "counters": self.counters, "status": self.status,
             "waiting_until": self.waiting_until}, self.current, self.status)
        self.checkpoint_version = rec.version

    def trace(self, kind: str, step: Optional[str] = None, **detail: Any) -> None:
        with _store().tx() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_trace WHERE run_id = ?",
                             (self.run_id,)).fetchone()[0]
            db.execute("INSERT INTO workflow_trace (run_id, seq, ts, step, kind, detail) VALUES (?,?,?,?,?,?)",
                       (self.run_id, seq, time.time(), step, kind,
                        json.dumps(detail, ensure_ascii=False, default=str)))
        # The same entry as a nexa.message/1.0 event in the run's conversation.
        ev = bus.emit(kind, correlation_id=self.run_id, trace_id=self.trace_id, sender=ORCHESTRATOR,
                      causation_id=self.last_message, principal=self.on_behalf_of,
                      payload=mc.RunEvent(workflow=self.workflow_id, run_id=self.run_id, step=step, seq=seq,
                                          detail=json.loads(json.dumps(detail, default=str))).model_dump())
        self.last_message = ev.message_id


def load_run(run_id: str) -> Optional[Run]:
    rows = _store().query("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,))
    return Run(dict(rows[0])) if rows else None


def trace_of(run_id: str) -> list[dict[str, Any]]:
    return [{"seq": r["seq"], "ts": r["ts"], "step": r["step"], "kind": r["kind"],
             "detail": json.loads(r["detail"])}
            for r in _store().query("SELECT * FROM workflow_trace WHERE run_id = ? ORDER BY seq", (run_id,))]


def view(run: Run, *, with_trace: bool = False) -> dict[str, Any]:
    out = {"run_id": run.run_id, "workflow": run.workflow_id, "workflow_version": run.workflow_version,
           "status": run.status, "initiator": run.principal.subject, "trace_id": run.trace_id,
           "input": run.input, "current_step": run.current, "output": run.output, "error": run.error,
           "counters": run.counters, "created_at": run.created_at,
           "steps": {k: {f: v for f, v in s.items() if f != "prompt"} for k, s in run.ctx["steps"].items()}}
    if run.status == "interrupted":
        out["resumable"] = resumable(run) is None
        out["not_resumable_because"] = resumable(run)
    if run.ctx.get("_session"):
        out["session"] = run.ctx["_session"]["id"]
    if run.status == "waiting_approval" and run.wf is not None:
        cp = run.wf.step(run.current)
        out["checkpoint"] = {"step": run.current, "title": cp.title, "approver_roles": cp.approver_roles,
                             "four_eyes": cp.four_eyes, "show": run.ctx["steps"].get(run.current, {}).get("show"),
                             "expires_at": run.waiting_until}
    if with_trace:
        out["trace"] = trace_of(run.run_id)
    return out


def may_view(run: Run, principal: Principal) -> bool:
    if principal.subject == run.principal.subject or set(principal.roles) & OVERSIGHT_ROLES:
        return True
    if run.status == "waiting_approval" and run.wf is not None:
        # A reviewer sees only runs of their own school (NEXA-M3-007).
        return bool(set(principal.roles) & set(run.wf.step(run.current).approver_roles)) and \
            pdp.same_tenant(run.principal.school_id, principal)
    return False


def list_runs(principal: Principal, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM workflow_runs", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit * 5)
    out = []
    for r in _store().query(sql, args):
        run = Run(dict(r))
        if may_view(run, principal):
            out.append(view(run))
        if len(out) >= limit:
            break
    return out


def mark_interrupted() -> int:
    """At start: runs that were executing when the runtime stopped. They are
    not continued blindly: the person who started one resumes it (resume()),
    from its last checkpoint, with their own session (NEXA-M3-006)."""
    rows = _store().query("SELECT run_id FROM workflow_runs WHERE status = 'running'")
    for r in rows:
        run = load_run(r["run_id"])
        run.status = "interrupted"
        run.error = "the Agent Runtime restarted while the run was executing; it can be resumed"
        with _store().tx() as db:
            db.execute("UPDATE workflow_runs SET status = 'interrupted', error = ?, updated_at = ? WHERE run_id = ?",
                       (run.error, time.time(), run.run_id))
        run.trace("run.interrupted", run.current)
    return len(rows)


def resumable(run: Run) -> Optional[str]:
    """None if the run can resume; otherwise why not."""
    if run.status != "interrupted":
        return f"the run is {run.status}"
    if run.wf is None:
        return f"workflow {run.workflow_id} is no longer loaded"
    cp = state.latest_valid_checkpoint(run.run_id)
    if cp is None:
        return "no valid checkpoint"
    if cp[1].get("fingerprint") != run.wf.fingerprint():
        return "the workflow changed since the run started; it cannot resume on a different definition"
    return None


async def resume(run_id: str, principal: Principal, *, auth: Optional[str] = None,
                 wait: bool = True) -> dict[str, Any]:
    """Continue an interrupted run from its newest valid checkpoint. The step
    that was executing runs again: work it had finished is not redone, because
    every agent and tool request repeats its idempotency key (NEXA-M3-005)."""
    run = load_run(run_id)
    if run is None or principal.subject != (run.principal.subject if run else None):
        if run is not None:
            state.log_access(principal, "resume", "run", run_id, decision="deny", reason="not the run's initiator")
        raise OrchestrationError(404, f"Unknown run: {run_id}")
    why = resumable(run)
    if why:
        state.log_access(principal, "resume", "run", run_id, decision="deny", reason=why)
        raise OrchestrationError(409, f"Run {run_id} cannot resume: {why}")
    version, scope, data = state.latest_valid_checkpoint(run_id)
    with _store().tx() as db:
        changed = db.execute("UPDATE workflow_runs SET status = 'running', updated_at = ? WHERE run_id = ? AND "
                             "status = 'interrupted'", (time.time(), run_id)).rowcount
    if not changed:
        raise OrchestrationError(409, f"Run {run_id} is already being resumed")
    run.ctx, run.current, run.counters = data["ctx"], data["current"], data["counters"]
    run.waiting_until = data.get("waiting_until")
    run.status, run.error, run.auth = "running", None, auth
    if data.get("status") == "waiting_approval":
        run.status = "waiting_approval"     # it was paused at a checkpoint: it waits again
    state.log_access(principal, "resume", "run", run_id, version=version, keys=["ctx", "current", "counters"])
    run.trace("run.resumed", run.current, checkpoint=version, checkpoint_status=data.get("status"))
    run.save()
    if run.status == "running":
        if wait:
            await advance(run)
        else:
            task = asyncio.create_task(advance(run))
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)
    return view(load_run(run_id))


# ---------------------------------------------------------------- starting and deciding

_TASKS: set[asyncio.Task] = set()


async def start(workflow_id: str, principal: Principal, inputs: dict[str, Any], *, auth: Optional[str] = None,
                wait: bool = True, trace_id: Optional[str] = None,
                idempotency_key: Optional[str] = None, session_id: Optional[str] = None) -> dict[str, Any]:
    if runtime.active_run() is not None:
        raise OrchestrationError(409, "a workflow cannot be started from inside an agent run")
    wf = WORKFLOWS.get(workflow_id)
    if wf is None:
        raise OrchestrationError(404, f"Unknown workflow: {workflow_id}")
    if wf.state != "active":
        raise OrchestrationError(409 if wf.state == "draft" else 410, f"Workflow {workflow_id} is {wf.state}")
    if not (set(principal.roles) & set(wf.allowed_roles)) or not can(principal, wf.run_permission):
        audit_event(action=f"workflow.run:{workflow_id}", decision="deny", principal=principal.subject)
        raise OrchestrationError(403, "Not permitted")
    problems = [f"unknown input {k!r}" for k in inputs if k not in wf.input]
    for k, kind in wf.input.items():
        v = inputs.get(k)
        if not isinstance(v, str) or not v.strip():
            problems.append(f"input {k!r} is required text")
        elif len(v) > MAX_TEXT[kind]:
            problems.append(f"input {k!r} is longer than {MAX_TEXT[kind]} characters")
    if problems:
        raise OrchestrationError(422, problems)
    if session_id and not wf.session:
        raise OrchestrationError(422, f"workflow {workflow_id} does not use session state")
    snapshot: dict[str, Any] = {}
    session_ref = None
    if session_id:
        try:
            rec = state.read_session(session_id, principal, keys=wf.session.read, via=wf.id, trace_id=trace_id)
        except sc.StateError as exc:
            raise OrchestrationError(exc.status_code, exc.detail)
        snapshot, session_ref = rec.data, {"id": session_id, "version": rec.version}
    # Idempotent start (NEXA-M3-005): the same key from the same user with the
    # same input returns the run already started; with other input it is refused.
    ikey = f"run-start/{principal.subject}/{idempotency_key}" if idempotency_key else None
    if ikey:
        dig = mc.digest({"workflow": workflow_id, "input": inputs})
        prior = _store().query("SELECT * FROM message_idempotency WHERE idempotency_key = ?", (ikey,))
        if prior:
            if prior[0]["payload_digest"] != dig:
                raise OrchestrationError(409, "Idempotency-Key was already used for a different run")
            existing = load_run(prior[0]["request_id"])
            if existing is not None:
                return view(existing) | {"duplicate": True}
    run_id, now = "wfr_" + uuid.uuid4().hex[:20], time.time()
    trace_id = trace_id or new_trace_id()
    with _store().tx() as db:
        db.execute("INSERT INTO workflow_runs (run_id, workflow_id, workflow_version, workflow_fingerprint, status, "
                   "initiator, initiator_roles, trace_id, input, context, current_step, counters, created_at, "
                   "updated_at) VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?,?)",
                   (run_id, wf.id, wf.version, wf.fingerprint(), principal.subject, json.dumps(list(principal.roles)),
                    trace_id, json.dumps(inputs, ensure_ascii=False),
                    json.dumps({"steps": {}, **({"session": snapshot, "_session": session_ref} if session_ref else {}),
                                "_tenant": {"school_id": principal.school_id,
                                            "class_ids": principal.attrs.get("class_ids")}},
                               ensure_ascii=False), wf.start,
                    json.dumps({"steps": 0, "agent_calls": 0, "running_s": 0.0}), now, now))
    if ikey:
        with _store().tx() as db:
            db.execute("INSERT INTO message_idempotency (idempotency_key, payload_digest, request_id, completed_at) "
                       "VALUES (?,?,?,?)", (ikey, dig, run_id, now))
    run = load_run(run_id)
    run.auth = auth
    run.save()                     # checkpoint 1: nothing done yet, resume starts at the first step
    run.trace("run.started", None, workflow=wf.id, version=wf.version, fingerprint=wf.fingerprint(),
              pattern=wf.pattern, initiator=principal.subject, roles=list(principal.roles),
              budget=wf.budget.model_dump(), location=presence.location())
    audit_event(action=f"workflow.run:{workflow_id}", decision="allow", principal=principal.subject,
                run_id=run_id, trace_id=trace_id)
    if wait:
        await advance(run)
    else:
        task = asyncio.create_task(advance(run))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    return view(load_run(run_id))


async def decide(run_id: str, principal: Principal, decision: str, comment: str = "", *,
                 wait: bool = True) -> dict[str, Any]:
    run = load_run(run_id)
    if run is None:
        raise OrchestrationError(404, f"Unknown run: {run_id}")
    if run.status != "waiting_approval":
        raise OrchestrationError(409, f"Run {run_id} is {run.status}, not waiting for a decision")
    cp = run.wf.step(run.current)
    if decision not in ("approve", "reject"):
        raise OrchestrationError(422, "decision is approve or reject")
    if run.waiting_until and time.time() > run.waiting_until:
        run.status, run.error = "expired", f"no decision before {time.strftime('%Y-%m-%d %H:%M', time.gmtime(run.waiting_until))} UTC"
        run.save()
        run.trace("checkpoint.expired", cp.id)
        raise OrchestrationError(409, f"Run {run_id} expired at its checkpoint")
    tenant = pdp.authorize_tenant("decide", run.principal.school_id, principal, f"run:{run_id}",
                                  trace_id=run.trace_id, run_id=run_id)
    if not tenant.allow:
        run.trace("checkpoint.refused", cp.id, by=principal.subject, reason="tenancy",
                  decision_id=tenant.decision_id)
        raise OrchestrationError(403, "This run belongs to another school")
    if not set(principal.roles) & set(cp.approver_roles):
        run.trace("checkpoint.refused", cp.id, by=principal.subject, reason="role")
        audit_event(action=f"workflow.decide:{run.workflow_id}", decision="deny", principal=principal.subject,
                    run_id=run_id)
        raise OrchestrationError(403, f"Only {', '.join(cp.approver_roles)} may decide this checkpoint")
    if cp.four_eyes and principal.subject == run.principal.subject:
        run.trace("checkpoint.refused", cp.id, by=principal.subject, reason="four_eyes")
        raise OrchestrationError(403, "four eyes: the person who started the run may not decide it")
    # Only one decision is accepted, even if two arrive at once.
    with _store().tx() as db:
        changed = db.execute("UPDATE workflow_runs SET status = 'running' WHERE run_id = ? AND "
                             "status = 'waiting_approval'", (run_id,)).rowcount
    if not changed:
        raise OrchestrationError(409, f"Run {run_id} was decided by someone else")
    run.status = "running"
    review = {"decision": decision, "by": principal.subject, "roles": list(principal.roles), "comment": comment,
              "at": time.time()}
    run.ctx["review"] = review
    run.ctx["steps"][cp.id].update(review, output=decision)
    run.ctx["last"] = run.ctx["steps"][cp.id]
    run.trace("checkpoint.decided", cp.id, **review)
    audit_event(action=f"workflow.decide:{run.workflow_id}", decision="allow", principal=principal.subject,
                run_id=run_id, outcome=decision)
    run.waiting_until = None
    run.current = cp.on_approve if decision == "approve" else cp.on_reject
    if decision == "reject" and run.current == wfm.END:
        _finish(run, "rejected", None)
        return view(load_run(run_id))
    run.save()
    if wait:
        await advance(run)
    else:
        task = asyncio.create_task(advance(run))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    return view(load_run(run_id))


def _write_session(run: Run) -> None:
    """Hand the declared keys to the session when the run succeeds."""
    ref = run.ctx.get("_session")
    if not (ref and run.wf.session and run.wf.session.write):
        return
    updates = {k: render(t, run.template_ctx)[:2000] for k, t in run.wf.session.write.items()}
    expected = ref["version"]
    for attempt in (1, 2):
        try:
            rec = state.write_session(ref["id"], run.principal, updates, expected_version=expected, via=run.wf.id,
                                      allowed_keys=set(run.wf.session.write), trace_id=run.trace_id)
            run.trace("session.written", None, session=ref["id"], version=rec.version, keys=sorted(updates))
            return
        except sc.StateError as exc:
            if exc.status_code == 409 and attempt == 1:
                # Another run wrote in between. Its other keys stand; ours are
                # declared by this workflow, so write them on the new version.
                cur = state.load_session_raw(ref["id"])
                if cur is None:
                    break
                run.trace("session.conflict", None, session=ref["id"], expected=expected, found=cur.version)
                expected = cur.version
                continue
            run.trace("session.write_refused", None, session=ref["id"], status=exc.status_code,
                      reason=str(exc.detail)[:300])
            return


def _finish(run: Run, status: str, error: Optional[str]) -> None:
    run.status, run.error = status, error
    if status == "succeeded":
        run.output = render(run.wf.output, run.template_ctx)
        _write_session(run)
    run.current = None
    run.save()
    run.trace("run." + status, None, error=error, counters=run.counters,
              output_chars=len(run.output or ""))
    audit_event(action=f"workflow.{status}:{run.workflow_id}", decision="allow", principal=run.principal.subject,
                run_id=run.run_id, trace_id=run.trace_id)
    obs.run_finished(run, status)


# ---------------------------------------------------------------- the loop

async def advance(run: Run) -> None:
    """Run steps until the end, a checkpoint, a failure or the budget."""
    wf = run.wf
    if wf is None:
        _finish(run, "failed", f"workflow {run.workflow_id} is no longer loaded")
        return
    segment_start = time.monotonic()
    base = run.counters["running_s"]

    def elapsed() -> float:
        return base + (time.monotonic() - segment_start)

    try:
        while run.status == "running" and run.current and run.current != wfm.END:
            step = wf.step(run.current)
            if run.counters["steps"] + 1 > wf.budget.max_steps:
                raise BudgetExceeded(f"max_steps {wf.budget.max_steps}")
            if elapsed() >= wf.budget.max_wall_s:
                raise BudgetExceeded(f"max_wall_s {wf.budget.max_wall_s}")
            run.counters["steps"] += 1
            run.trace("step.started", step.id, type=step.type, title=step.title)
            t0 = time.monotonic()
            try:
                nxt, result = await _execute(run, step, lambda: wf.budget.max_wall_s - elapsed())
            except BudgetExceeded:
                raise
            except StepError as exc:
                run.trace("step.failed", step.id, error=str(exc), latency_ms=int((time.monotonic() - t0) * 1000))
                if step.policy.on_error == "fail":
                    run.counters["running_s"] = elapsed()
                    _finish(run, "failed", f"step {step.id}: {exc}")
                    return
                run.ctx["steps"][step.id] = {"error": str(exc), "output": ""}
                run.ctx["last"] = run.ctx["steps"][step.id]
                run.trace("step.error_path", step.id, goto=step.policy.on_error)
                run.current = step.policy.on_error
                run.counters["running_s"] = elapsed()
                run.save()
                continue
            run.ctx["steps"][step.id] = result
            if step.type != "checkpoint":
                run.ctx["last"] = result
            run.counters["running_s"] = elapsed()
            if run.status == "waiting_approval":          # a checkpoint paused the run
                run.save()
                return
            run.trace("step.completed", step.id, next=nxt, latency_ms=int((time.monotonic() - t0) * 1000))
            run.current = nxt
            run.save()
        if run.status == "running":
            _finish(run, "succeeded", None)
    except BudgetExceeded as exc:
        run.counters["running_s"] = elapsed()
        _finish(run, "budget_exceeded", f"budget exhausted: {exc}")
    except Exception as exc:  # noqa: BLE001 - a bug must end the run, not leave it running
        log.exception("workflow run %s crashed", run.run_id)
        run.counters["running_s"] = elapsed()
        _finish(run, "failed", f"internal error: {type(exc).__name__}")


async def _execute(run: Run, step, remaining) -> tuple[str, dict[str, Any]]:
    ctx = run.template_ctx
    if isinstance(step, wfm.RouteStep):
        for i, rule in enumerate(step.rules):
            if all(holds(c, ctx) for c in rule.when):
                run.trace("route.decision", step.id, rule=i, goto=rule.goto, reason=rule.reason,
                          tested=[{"field": c.field, "value": _short(lookup(c.field, ctx))} for c in rule.when])
                return rule.goto, {"output": rule.goto, "rule": i, "reason": rule.reason}
        run.trace("route.decision", step.id, rule=None, goto=step.default, reason="no rule matched: default")
        return step.default, {"output": step.default, "rule": None, "reason": "default"}
    if isinstance(step, wfm.RespondStep):
        return step.next, {"output": render(step.output, ctx)}
    if isinstance(step, wfm.CheckpointStep):
        show = render(step.show, ctx)
        run.status, run.waiting_until = "waiting_approval", time.time() + step.expires_after_s
        run.trace("checkpoint.waiting", step.id, approver_roles=step.approver_roles, four_eyes=step.four_eyes,
                  expires_at=run.waiting_until, show_chars=len(show))
        return step.on_approve, {"show": show, "output": ""}
    if isinstance(step, wfm.KnowledgeStep):
        return step.next, await _with_policy(run, step, remaining, lambda t: _knowledge(run, step, ctx, t))
    if isinstance(step, wfm.AgentStep):
        return step.next, await _agent_step(run, step, ctx, remaining)
    if isinstance(step, wfm.ToolStep):
        return step.next, await _with_policy(run, step, remaining, lambda t: _tool(run, step, ctx, t))
    if isinstance(step, wfm.DelegateStep):
        return step.next, await _delegate(run, step, ctx, remaining)
    raise StepError(f"unknown step type {step.type}", retry=False)


async def _with_policy(run: Run, step, remaining, attempt_fn):
    """Per-step timeout and retries (the step's policy), inside the run budget."""
    last: Optional[StepError] = None
    for attempt in range(1, step.policy.max_attempts + 1):
        left = remaining()
        if left <= 0:
            raise BudgetExceeded("max_wall_s")
        timeout = min(step.policy.timeout_s, left)
        try:
            return await asyncio.wait_for(attempt_fn(timeout), timeout=timeout)
        except asyncio.TimeoutError:
            last = StepError(f"timed out after {timeout:.0f}s", retry=True)
        except StepError as exc:
            last = exc
        run.trace("step.attempt_failed", step.id, attempt=attempt, error=str(last), retry=last.retry)
        if not last.retry:
            break
        if attempt < step.policy.max_attempts:
            await asyncio.sleep(min(step.policy.backoff_s, max(0.0, remaining())))
    raise last


# ---------------------------------------------------------------- agents

def _resolve(run: Run, step: wfm.AgentStep) -> list[str]:
    """The agents to try, in order: the named (or discovered) one, then fallbacks."""
    if step.agent:
        first = [step.agent]
    else:
        found = registry.REGISTRY.discover(run.principal, capability=step.capability, location=presence.location())
        if not found:
            raise StepError(f"no runnable, healthy agent with capability {step.capability!r} for this user",
                            retry=False)
        first = [found[0]["agent_id"]]
        run.trace("agent.discovered", step.id, capability=step.capability, chose=first[0],
                  candidates=[a["agent_id"] for a in found])
    return first + [a for a in step.fallback_agents if a not in first]


_AGENT_ERRORS = {403: "refused", 409: "refused", 410: "refused", 423: "refused", 404: "invalid", 422: "invalid",
                 429: "budget", 502: "unavailable", 503: "unavailable", 504: "timeout"}


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _error(code: str, reason: str, status: Optional[int] = None) -> mc.ErrorInfo:
    return mc.ErrorInfo(code=code, retryable=code in mc.RETRYABLE, reason=reason[:1000], status=status)


def _deadline(timeout: float) -> float:
    # A little inside the step's own timeout, so the bus answers first.
    return max(0.05, timeout * 0.98 - 0.05)


async def _call_agent(run: Run, step_id: str, agent: str, prompt: str, *, temperature=None, max_tokens=None,
                      timeout: float, role: str = "agent", key: Optional[str] = None, max_attempts: int = 1,
                      cause: Optional[str] = None) -> dict[str, Any]:
    """One agent call, as a nexa.message/1.0 request from the orchestrator to the
    agent and its result or error back (NEXA-M3-005)."""
    if run.counters["agent_calls"] + 1 > run.wf.budget.max_agent_calls:
        raise BudgetExceeded(f"max_agent_calls {run.wf.budget.max_agent_calls}")
    run.counters["agent_calls"] += 1
    key = key or f"{run.run_id}/{step_id}/{role}/{agent}"
    run.attempts[key] = attempt = run.attempts.get(key, 0) + 1
    spec = registry.AGENTS.get(agent)
    span = _deadline(timeout)
    req = mc.make("request", "agent.task", correlation_id=run.run_id, trace_id=run.trace_id, sender=ORCHESTRATOR,
                  receiver=mc.Party(kind="agent", id=agent, version=spec.version if spec else None),
                  payload=mc.AgentTask(role=role, workflow=run.workflow_id, step=step_id, prompt=prompt,
                                       temperature=temperature, max_tokens=max_tokens).model_dump(exclude_none=True),
                  causation_id=cause or run.last_message, principal=run.on_behalf_of,
                  permission=spec.required_permission if spec else "model.invoke",
                  deadline=time.time() + span, timeout_s=span, attempt=min(attempt, 10),
                  max_attempts=max(min(attempt, 10), max_attempts), idempotency_key=key)
    caller = {"workflow": run.workflow_id, "workflow_version": run.workflow_version, "run_id": run.run_id,
              "step": step_id, "as": role, "message_id": req.message_id}
    t0 = time.monotonic()

    async def deliver(msg: mc.Message, left: float) -> bus.Delivered:
        try:
            resp = await runtime.run_agent(agent, run.principal, [{"role": "user", "content": prompt}], temperature,
                                           max_tokens, run.trace_id, caller=caller)
        except runtime.AgentRuntimeError as exc:
            return bus.Delivered(error=_error(_AGENT_ERRORS.get(exc.status_code, "internal"), str(exc.detail),
                                              exc.status_code))
        if not (resp.output or "").strip():
            # An empty answer is an invalid response: never passed on, never
            # recorded as done (NEXA-M3-009).
            return bus.Delivered(error=_error("invalid", f"{agent} returned an empty response"))
        x = resp.x_nexa or {}
        u = resp.usage or {}
        return bus.Delivered(sender_version=x.get("agent_version"), payload=mc.AgentResultV2(
            output=resp.output or "", agent_version=x.get("agent_version"), manifest=x.get("manifest"),
            model=x.get("model") or resp.model, attempts=x.get("attempts"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            prompt_tokens=_int(u.get("prompt_tokens")), completion_tokens=_int(u.get("completion_tokens")),
            fallback_model=x.get("fallback_model"), redacted=x.get("redacted")).model_dump(exclude_none=True))

    reply = await bus.request(req, deliver)
    run.last_message = reply.message_id
    ms = int((time.monotonic() - t0) * 1000)
    if reply.kind == "error":
        e = reply.error
        obs.agent_call(run, step_id, agent, role, req.message_id, ms, ok=False, code=e.code)
        run.trace("agent.call", step_id, agent=agent, role=role, ok=False, status=e.status, error=e.reason,
                  code=e.code, request=req.message_id, reply=reply.message_id)
        raise StepError(f"{agent}: {e.reason}", retry=e.retryable, status=e.status)
    p = reply.payload
    obs.agent_call(run, step_id, agent, role, req.message_id, ms, ok=True, model=p.get("model"),
                   prompt_tokens=p.get("prompt_tokens"), completion_tokens=p.get("completion_tokens"),
                   )
    rec = {"agent": agent, "version": p.get("agent_version"), "manifest": p.get("manifest"), "model": p.get("model"),
           "attempts": p.get("attempts"), "latency_ms": int((time.monotonic() - t0) * 1000),
           "trace_id": run.trace_id, "request": req.message_id, "reply": reply.message_id}
    rec.update(prompt_tokens=p.get("prompt_tokens"), completion_tokens=p.get("completion_tokens"))
    run.trace("agent.call", step_id, role=role, ok=True, output_chars=len(p.get("output", "")), **rec)
    return {"output": p.get("output", ""), **rec}


async def _agent_step(run: Run, step: wfm.AgentStep, ctx, remaining) -> dict[str, Any]:
    prompt = render(step.prompt, ctx)
    candidates = _resolve(run, step)
    errors = []
    for i, agent in enumerate(candidates):
        if i:
            run.trace("agent.fallback", step.id, to=agent, after=candidates[i - 1], reason=errors[-1])
        try:
            res = await _with_policy(run, step, remaining, lambda t, a=agent: _call_agent(
                run, step.id, a, prompt, temperature=step.temperature, max_tokens=step.max_tokens, timeout=t,
                max_attempts=step.policy.max_attempts))
            return res | {"fallback_used": i > 0}
        except StepError as exc:
            errors.append(str(exc))
    raise StepError("; ".join(errors), retry=False)


# ---------------------------------------------------------------- bounded autonomy

CHOICE = re.compile(r"\{[^{}]*\}", re.S)


def parse_choice(text: str, allowed: set[str]) -> tuple[Optional[str], str, str]:
    """(action, task, problem). The coordinator must answer with one JSON object
    naming an allowed action; anything else is refused, never guessed."""
    for m in CHOICE.finditer(text or ""):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        action = str(obj.get("action", "")).strip().lower()
        task = str(obj.get("task", "")).strip()[:2000]
        if action == "done":
            return "done", "", ""
        if action not in allowed:
            return None, "", f"action {action!r} is not allowed"
        if not task:
            return None, "", "no task given"
        return action, task, ""
    return None, "", "no JSON object with an action"


async def _delegate(run: Run, step: wfm.DelegateStep, ctx, remaining) -> dict[str, Any]:
    allowed = set(step.delegates)
    menu = "\n".join(f"- {name}: {registry.AGENTS[a].description if a in registry.AGENTS else a}"
                     for name, a in step.delegates.items())
    done: list[dict[str, Any]] = []
    decisions = 0
    stop_reason = "limit"
    failed: Optional[str] = None
    while len(done) < step.max_delegations:
        work = "\n\n".join(f"[{i}] {d['action']} -> {d['agent']}\nTask: {d['task']}\nResult: {d['output'][:CARRY]}"
                           for i, d in enumerate(done, 1)) or "(nothing yet)"
        prompt = (f"{render(step.instructions, ctx)}\n\nYou may choose one of these actions:\n{menu}\n"
                  f"- done: the work is complete\n\nWork so far:\n{work}\n\n"
                  f"Actions left: {step.max_delegations - len(done)}. Reply with only one JSON object, for example "
                  f'{{"action": "{next(iter(step.delegates))}", "task": "what that agent should do"}} '
                  f'or {{"action": "done"}}.')
        try:
            choice = await _with_policy(run, step, remaining, lambda t: _call_agent(
                run, step.id, step.coordinator, prompt, temperature=step.temperature, max_tokens=300, timeout=t,
                role="coordinator", key=f"{run.run_id}/{step.id}/coordinator/{decisions + 1}",
                max_attempts=step.policy.max_attempts))
        except StepError as exc:
            if not done:
                raise
            failed = f"coordinator failed: {exc}"
            break
        decisions += 1
        action, task, problem = parse_choice(choice["output"], allowed)
        run.trace("delegate.choice", step.id, decision=decisions, coordinator=step.coordinator,
                  raw=_short(choice["output"], 600), accepted=action is not None, action=action, task=_short(task),
                  problem=problem or None)
        # The orchestrator's decision on the coordinator's proposal, as a typed
        # event caused by the coordinator's result: the delegate's request is
        # caused by this decision, never by the coordinator (NEXA-M3-005).
        decided = bus.emit("delegation.choice", correlation_id=run.run_id, trace_id=run.trace_id,
                           sender=ORCHESTRATOR, causation_id=choice["reply"], principal=run.on_behalf_of,
                           payload=mc.DelegationChoice(
                               step=step.id, decision=decisions, proposed=_short(choice["output"], 300),
                               accepted=action is not None, action=action,
                               delegate=step.delegates.get(action) if action else None,
                               problem=problem or None).model_dump(exclude_none=True))
        run.last_message = decided.message_id
        if action is None:
            stop_reason = f"invalid choice: {problem}"
            break
        if action == "done":
            stop_reason = "coordinator chose done"
            break
        agent = step.delegates[action]
        # The coordinator proposed; the agent policy decides (NEXA-M3-007).
        coord = registry.AGENTS.get(step.coordinator)
        verdict = pdp.authorize_delegate(step.coordinator, coord.version if coord else None, agent, run.principal,
                                         trace_id=run.trace_id, run_id=run.run_id)
        if not verdict.allow:
            run.trace("delegate.refused", step.id, coordinator=step.coordinator, delegate=agent, rule=verdict.rule,
                      reason=verdict.reason, decision_id=verdict.decision_id)
            stop_reason = f"policy: {verdict.reason}"
            break
        try:
            res = await _with_policy(run, step, remaining, lambda t: _call_agent(
                run, step.id, agent, render(step.delegate_prompt, {**ctx, "task": task}), timeout=t,
                role="delegate", key=f"{run.run_id}/{step.id}/delegate/{len(done) + 1}",
                max_attempts=step.policy.max_attempts, cause=decided.message_id))
        except StepError as exc:
            if not done:
                raise
            failed = f"{action} by {agent} failed: {exc}"
            break
        done.append({"action": action, "agent": agent, "task": task, "output": res["output"],
                     "version": res["version"]})
    if failed:
        # Partial results (NEXA-M3-009): work already done is kept only if a
        # person reviews it before release, and is marked incomplete;
        # otherwise the step fails and nothing partial is released.
        if not human_review_follows(run.wf, step.id):
            raise StepError(f"{failed}; no human review follows, so the partial work is not kept", retry=False)
        stop_reason = failed
        run.trace("delegate.partial", step.id, delegations=len(done), reason=_short(failed, 300))
    run.trace("delegate.finished", step.id, delegations=len(done), decisions=decisions, reason=stop_reason)
    if not done:
        raise StepError(f"no work was delegated ({stop_reason})", retry=False)
    combined = "\n\n".join(f"## {d['action'].replace('_', ' ').capitalize()}\n\n{d['output']}" for d in done)
    if failed:
        combined = ("INCOMPLETE: part of this material could not be prepared " f"({_short(failed, 200)}). "
                    "Review it before use.\n\n" + combined)
    return {"output": combined, "last_output": done[-1]["output"], "delegations": done, "decisions": decisions,
            "stopped": stop_reason, "partial": bool(failed)}


def human_review_follows(wf: wfm.Workflow, step_id: str) -> bool:
    """Whether every path from this step reaches a human checkpoint before the end."""
    def reaches(sid: str, seen: frozenset) -> bool:
        if sid == wfm.END or sid in seen:
            return False
        st = wf.step(sid)
        if isinstance(st, wfm.CheckpointStep):
            return True
        nxt = [r.goto for r in st.rules] + [st.default] if isinstance(st, wfm.RouteStep) else \
            [getattr(st, "next", wfm.END)]
        return all(reaches(n, seen | {sid}) for n in nxt)
    st = wf.step(step_id)
    nxt = getattr(st, "next", wfm.END)
    return reaches(nxt, frozenset())


# ---------------------------------------------------------------- tools (NEXA-M3-004)

_TOOL_ERRORS = {401: "refused", 403: "refused", 409: "refused", 410: "refused", 423: "refused", 404: "invalid",
                422: "invalid", 429: "budget", 502: "unavailable", 503: "unavailable", 504: "timeout"}


async def _tool(run: Run, step: wfm.ToolStep, ctx, timeout: float) -> dict[str, Any]:
    """One tool call through the Tool & Connector Gateway, for the step's agent,
    as the calling user: a nexa.message/1.0 tool.task request and its result."""
    if not run.auth:
        raise StepError("a tool step needs the user's own session", retry=False)
    args = {k: render(v, ctx).strip() for k, v in step.arguments.items()}
    key = f"{run.run_id}/{step.id}/tool/{step.tool}"
    run.attempts[key] = attempt = run.attempts.get(key, 0) + 1
    spec = tools.TOOLS.get(step.tool)
    span = _deadline(timeout)
    req = mc.make("request", "tool.task", correlation_id=run.run_id, trace_id=run.trace_id, sender=ORCHESTRATOR,
                  receiver=mc.Party(kind="tool", id=step.tool, version=spec.version if spec else None),
                  payload=mc.ToolTask(tool=step.tool, for_agent=step.agent, step=step.id, arguments=args,
                                      scopes=step.scopes).model_dump(exclude_none=True),
                  causation_id=run.last_message, principal=run.on_behalf_of,
                  permission=spec.required_permission if spec else "model.invoke",
                  deadline=time.time() + span, timeout_s=span, attempt=min(attempt, 10),
                  max_attempts=max(min(attempt, 10), step.policy.max_attempts), idempotency_key=key)
    t0 = time.monotonic()

    async def deliver(msg: mc.Message, left: float) -> bus.Delivered:
        try:
            out = await tools.invoke_tool(step.agent, step.tool, run.principal, args, user_token=run.auth,
                                          scopes=step.scopes, run_id=run.run_id, step=step.id, trace_id=run.trace_id)
        except tools.ToolError as exc:
            reason = exc.detail + (f" (call {exc.call_id})" if exc.call_id else "")
            return bus.Delivered(error=_error(_TOOL_ERRORS.get(exc.status_code, "internal"), reason, exc.status_code))
        return bus.Delivered(sender_version=out.get("version"), payload=mc.ToolResult(
            call_id=out.get("call_id"), tool_version=out.get("version"), scopes=out.get("scopes") or [],
            result=out.get("result") or {}, latency_ms=int((time.monotonic() - t0) * 1000)).model_dump())

    reply = await bus.request(req, deliver)
    run.last_message = reply.message_id
    obs.tool_call(run, step.id, step.tool, step.agent, req.message_id, int((time.monotonic() - t0) * 1000),
                  ok=reply.kind != "error", code=reply.error.code if reply.error else None)
    if reply.kind == "error":
        e = reply.error
        run.trace("tool.call", step.id, agent=step.agent, tool=step.tool, ok=False, status=e.status, error=e.reason,
                  code=e.code, arguments=_short(json.dumps(args, ensure_ascii=False)), request=req.message_id)
        raise StepError(f"{step.tool}: {e.reason}", retry=e.retryable, status=e.status)
    p = reply.payload
    result = p.get("result") or {}
    rec = {"tool": step.tool, "version": p.get("tool_version"), "call_id": p.get("call_id"),
           "scopes": p.get("scopes"), "agent": step.agent, "latency_ms": int((time.monotonic() - t0) * 1000),
           "request": req.message_id, "reply": reply.message_id}
    run.trace("tool.call", step.id, ok=True, arguments=_short(json.dumps(args, ensure_ascii=False)), **rec)
    return {"output": result.get("text", ""), "result": result, **rec}


# ---------------------------------------------------------------- knowledge

async def _knowledge(run: Run, step: wfm.KnowledgeStep, ctx, timeout: float) -> dict[str, Any]:
    """A grounded answer from the Knowledge Service, as the calling user (their
    own token: the Knowledge Service applies their access)."""
    if not run.auth:
        raise StepError("a knowledge step needs the user's own session", retry=False)
    url = os.environ.get("NEXA_KNOWLEDGE_URL", "http://127.0.0.1:8103").rstrip("/") + "/v1/knowledge/answer"
    body = {"question": render(step.question, ctx)[:2000], "general": bool(step.general)}
    if step.audience:
        body["audience"] = step.audience
    t0 = time.monotonic()
    try:
        data = await knowledge_answer(url, body, run.auth, run.trace_id, timeout)
    except httpx.TimeoutException:
        raise StepError("knowledge service timed out", retry=True)
    except httpx.HTTPError as exc:
        raise StepError(f"knowledge service unreachable: {type(exc).__name__}", retry=True)
    if data.get("_status", 200) != 200:
        raise StepError(f"knowledge service answered {data['_status']}", retry=data["_status"] >= 500)
    ev = data.get("evidence") or []
    rec = {"output": data.get("answer", ""), "answer": data.get("answer", ""), "status": data.get("status"),
           "confidence": data.get("confidence"), "citations": len(data.get("citations") or ev),
           "latency_ms": int((time.monotonic() - t0) * 1000)}
    run.trace("knowledge.call", step.id, status=rec["status"], confidence=rec["confidence"],
              citations=rec["citations"], latency_ms=rec["latency_ms"])
    return rec


async def knowledge_answer(url: str, body: dict[str, Any], auth: str, trace_id: str, timeout: float) -> dict:
    from v1.observability import TRACE_HEADER
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=body, headers={"Authorization": auth, TRACE_HEADER: trace_id})
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        return {"_status": r.status_code}
    return data


def _short(v: Any, n: int = 200) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "…"
    return v
