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
from . import workflow as wfm

log = logging.getLogger("nexa.agent_runtime.orchestration")

TERMINAL = {"succeeded", "rejected", "failed", "budget_exceeded", "expired", "interrupted"}
OVERSIGHT_ROLES = {"admin", "platform_admin", "auditor", "ai_engineer"}
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
    WORKFLOWS.clear()
    WORKFLOWS.update(found)
    return found


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
        self.principal = Principal(row["initiator"], tuple(json.loads(row["initiator_roles"])), "user")
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

    @property
    def template_ctx(self) -> dict[str, Any]:
        return {"input": self.input, **self.ctx}

    def save(self) -> None:
        with _store().tx() as db:
            db.execute("UPDATE workflow_runs SET status = ?, context = ?, current_step = ?, output = ?, error = ?, "
                       "counters = ?, waiting_until = ?, updated_at = ? WHERE run_id = ?",
                       (self.status, json.dumps(self.ctx, ensure_ascii=False), self.current, self.output,
                        self.error, json.dumps(self.counters), self.waiting_until, time.time(), self.run_id))

    def trace(self, kind: str, step: Optional[str] = None, **detail: Any) -> None:
        with _store().tx() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM workflow_trace WHERE run_id = ?",
                             (self.run_id,)).fetchone()[0]
            db.execute("INSERT INTO workflow_trace (run_id, seq, ts, step, kind, detail) VALUES (?,?,?,?,?,?)",
                       (self.run_id, seq, time.time(), step, kind,
                        json.dumps(detail, ensure_ascii=False, default=str)))


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
        return bool(set(principal.roles) & set(run.wf.step(run.current).approver_roles))
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
    """At start: runs that were executing when the runtime stopped cannot be
    resumed safely (an agent call may have been half done)."""
    rows = _store().query("SELECT run_id FROM workflow_runs WHERE status = 'running'")
    for r in rows:
        run = load_run(r["run_id"])
        run.status, run.error = "interrupted", "the Agent Runtime restarted while the run was executing"
        run.save()
        run.trace("run.interrupted", run.current)
    return len(rows)


# ---------------------------------------------------------------- starting and deciding

_TASKS: set[asyncio.Task] = set()


async def start(workflow_id: str, principal: Principal, inputs: dict[str, Any], *, auth: Optional[str] = None,
                wait: bool = True, trace_id: Optional[str] = None) -> dict[str, Any]:
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
    run_id, now = "wfr_" + uuid.uuid4().hex[:20], time.time()
    trace_id = trace_id or new_trace_id()
    with _store().tx() as db:
        db.execute("INSERT INTO workflow_runs (run_id, workflow_id, workflow_version, workflow_fingerprint, status, "
                   "initiator, initiator_roles, trace_id, input, context, current_step, counters, created_at, "
                   "updated_at) VALUES (?,?,?,?,'running',?,?,?,?,?,?,?,?,?)",
                   (run_id, wf.id, wf.version, wf.fingerprint(), principal.subject, json.dumps(list(principal.roles)),
                    trace_id, json.dumps(inputs, ensure_ascii=False), json.dumps({"steps": {}}), wf.start,
                    json.dumps({"steps": 0, "agent_calls": 0, "running_s": 0.0}), now, now))
    run = load_run(run_id)
    run.auth = auth
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


def _finish(run: Run, status: str, error: Optional[str]) -> None:
    run.status, run.error = status, error
    if status == "succeeded":
        run.output = render(run.wf.output, run.template_ctx)
    run.current = None
    run.save()
    run.trace("run." + status, None, error=error, counters=run.counters,
              output_chars=len(run.output or ""))
    audit_event(action=f"workflow.{status}:{run.workflow_id}", decision="allow", principal=run.principal.subject,
                run_id=run.run_id, trace_id=run.trace_id)


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


async def _call_agent(run: Run, step_id: str, agent: str, prompt: str, *, temperature=None, max_tokens=None,
                      timeout: float, role: str = "agent") -> dict[str, Any]:
    if run.counters["agent_calls"] + 1 > run.wf.budget.max_agent_calls:
        raise BudgetExceeded(f"max_agent_calls {run.wf.budget.max_agent_calls}")
    run.counters["agent_calls"] += 1
    caller = {"workflow": run.workflow_id, "workflow_version": run.workflow_version, "run_id": run.run_id,
              "step": step_id, "as": role}
    t0 = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            runtime.run_agent(agent, run.principal, [{"role": "user", "content": prompt}], temperature, max_tokens,
                              run.trace_id, caller=caller),
            timeout=timeout)
    except asyncio.TimeoutError:
        run.trace("agent.call", step_id, agent=agent, role=role, ok=False, error=f"timeout {timeout:.0f}s")
        raise StepError(f"{agent} timed out", retry=True)
    except runtime.AgentRuntimeError as exc:
        run.trace("agent.call", step_id, agent=agent, role=role, ok=False, status=exc.status_code,
                  error=str(exc.detail))
        # Unavailable or timing out may pass; refused (403, disabled, not active) will not.
        raise StepError(f"{agent}: {exc.detail}", retry=exc.status_code in (502, 503, 504), status=exc.status_code)
    x = resp.x_nexa or {}
    rec = {"agent": agent, "version": x.get("agent_version"), "manifest": x.get("manifest"),
           "model": x.get("model"), "attempts": x.get("attempts"), "fallback_model": x.get("fallback_model"),
           "latency_ms": int((time.monotonic() - t0) * 1000), "trace_id": run.trace_id}
    run.trace("agent.call", step_id, role=role, ok=True, output_chars=len(resp.output or ""), **rec)
    return {"output": resp.output or "", **rec}


async def _agent_step(run: Run, step: wfm.AgentStep, ctx, remaining) -> dict[str, Any]:
    prompt = render(step.prompt, ctx)
    candidates = _resolve(run, step)
    errors = []
    for i, agent in enumerate(candidates):
        if i:
            run.trace("agent.fallback", step.id, to=agent, after=candidates[i - 1], reason=errors[-1])
        try:
            res = await _with_policy(run, step, remaining, lambda t, a=agent: _call_agent(
                run, step.id, a, prompt, temperature=step.temperature, max_tokens=step.max_tokens, timeout=t))
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
    while len(done) < step.max_delegations:
        work = "\n\n".join(f"[{i}] {d['action']} -> {d['agent']}\nTask: {d['task']}\nResult: {d['output'][:CARRY]}"
                           for i, d in enumerate(done, 1)) or "(nothing yet)"
        prompt = (f"{render(step.instructions, ctx)}\n\nYou may choose one of these actions:\n{menu}\n"
                  f"- done: the work is complete\n\nWork so far:\n{work}\n\n"
                  f"Actions left: {step.max_delegations - len(done)}. Reply with only one JSON object, for example "
                  f'{{"action": "{next(iter(step.delegates))}", "task": "what that agent should do"}} '
                  f'or {{"action": "done"}}.')
        choice = await _with_policy(run, step, remaining, lambda t: _call_agent(
            run, step.id, step.coordinator, prompt, temperature=step.temperature, max_tokens=300, timeout=t,
            role="coordinator"))
        decisions += 1
        action, task, problem = parse_choice(choice["output"], allowed)
        run.trace("delegate.choice", step.id, decision=decisions, coordinator=step.coordinator,
                  raw=_short(choice["output"], 600), accepted=action is not None, action=action, task=_short(task),
                  problem=problem or None)
        if action is None:
            stop_reason = f"invalid choice: {problem}"
            break
        if action == "done":
            stop_reason = "coordinator chose done"
            break
        agent = step.delegates[action]
        res = await _with_policy(run, step, remaining, lambda t: _call_agent(
            run, step.id, agent, render(step.delegate_prompt, {**ctx, "task": task}), timeout=t,
            role="delegate"))
        done.append({"action": action, "agent": agent, "task": task, "output": res["output"],
                     "version": res["version"]})
    run.trace("delegate.finished", step.id, delegations=len(done), decisions=decisions, reason=stop_reason)
    if not done:
        raise StepError(f"no work was delegated ({stop_reason})", retry=False)
    combined = "\n\n".join(f"## {d['action'].replace('_', ' ').capitalize()}\n\n{d['output']}" for d in done)
    return {"output": combined, "last_output": done[-1]["output"], "delegations": done, "decisions": decisions,
            "stopped": stop_reason}


# ---------------------------------------------------------------- tools (NEXA-M3-004)

async def _tool(run: Run, step: wfm.ToolStep, ctx, timeout: float) -> dict[str, Any]:
    """One tool call through the Tool & Connector Gateway, for the step's agent,
    as the calling user."""
    if not run.auth:
        raise StepError("a tool step needs the user's own session", retry=False)
    args = {k: render(v, ctx).strip() for k, v in step.arguments.items()}
    t0 = time.monotonic()
    try:
        out = await tools.invoke_tool(step.agent, step.tool, run.principal, args, user_token=run.auth,
                                      scopes=step.scopes, run_id=run.run_id, step=step.id, trace_id=run.trace_id)
    except tools.ToolError as exc:
        run.trace("tool.call", step.id, agent=step.agent, tool=step.tool, ok=False, status=exc.status_code,
                  error=exc.detail, call_id=exc.call_id, arguments=_short(json.dumps(args, ensure_ascii=False)))
        raise StepError(f"{step.tool}: {exc.detail}", retry=exc.status_code in (502, 503, 504),
                        status=exc.status_code)
    result = out.get("result") or {}
    rec = {"tool": step.tool, "version": out.get("version"), "call_id": out.get("call_id"),
           "scopes": out.get("scopes"), "agent": step.agent,
           "latency_ms": int((time.monotonic() - t0) * 1000)}
    run.trace("tool.call", step.id, ok=True, arguments=_short(json.dumps(args, ensure_ascii=False)),
              implementation=(out.get("x_nexa") or {}).get("implementation"), **rec)
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
