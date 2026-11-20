"""Reconstruct an agent execution end to end (NEXA-M3-008): nexa.trace/1.0.

Nothing new is recorded for this: the record is assembled from what each
layer already keeps under the run's correlation id and trace id:

  workflow_runs, workflow_trace    the run, its steps, routes, checkpoints, errors (M3-003)
  messages                         every request, result and error between the orchestrator
                                   and agents or tools, and the delegation decisions (M3-005)
  policy_decisions                 every allow and deny, with the rule (M3-007)
  state_access                     every read and write of run and session state (M3-006)
  Tool Gateway audit               every tool call as the gateway saw it (M3-004), fetched
                                   with the operator's own token and joined by call id

and returned as a tree of spans:

  run
   └ step                          one per step execution (a resumed step appears again)
      ├ agent call                 request → result or error: agent and version, manifest,
      │                            role (agent, coordinator, delegate), attempt, latency,
      │                            model, prompt and completion tokens, cost if configured
      ├ tool call                  request → result or error, joined with the gateway's record:
      │                            tool and version, scopes, implementation, connector, the
      │                            gateway's decision and check, and the retrieval references
      │                            (curriculum documents and passages cited)
      └ events                     routes, policy decisions, handoffs, checkpoints, errors

with the handoffs (coordinator result → orchestrator decision → delegate
request) as edges, and a summary (duration, calls, tokens, cost, retries,
duplicates, denials, errors).

Two views:

  operational  for operators (ai_engineer, admin, platform_admin, auditor):
               users are pseudonymised (a keyed hash: stable within this
               deployment, not reversible without its secret); prompts,
               answers, arguments, inputs, outputs and comments are replaced
               by their size and digest; curriculum references, agents,
               models, timings, decisions and errors stay
  forensic     for auditor, admin, platform_admin, with a stated reason:
               the same record with content and real subjects; every use is
               written to the state access log

In both, any string that looks like a credential is removed, and the
operational view also removes email addresses and phone numbers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any, Optional

TRACE_SCHEMA = "nexa.trace/1.0"
SERVICE_PREFIXES = ("system:", "svc:", "service:", "agent:", "runtime:")
CREDENTIAL = re.compile(r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})|(\bBearer\s+\S{12,})", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?675[ -]?)?7\d{3}[ -]?\d{4}(?!\d)")
CALL_ID = re.compile(r"\b(tc_[0-9a-f]{20})\b")


class View:
    def __init__(self, kind: str = "operational"):
        self.kind = kind
        key = (os.environ.get("NEXA_IDENTITY_SECRET") or "nexa-trace").encode()
        self._key = hmac.new(key, b"nexa-trace-pseudonym", hashlib.sha256).digest()

    @property
    def forensic(self) -> bool:
        return self.kind == "forensic"

    def who(self, subject: Optional[str]) -> Optional[str]:
        if not subject or self.forensic or subject.startswith(SERVICE_PREFIXES):
            return subject
        return "user#" + hmac.new(self._key, subject.encode(), hashlib.sha256).hexdigest()[:10]

    def content(self, value: Any) -> Any:
        if value is None or self.forensic:
            return value
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        return {"chars": len(text), "digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]}

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub(v) for v in obj]
        if isinstance(obj, str):
            s = CREDENTIAL.sub("[secret removed]", obj)
            if not self.forensic:
                s = PHONE.sub("[personal data removed]", EMAIL.sub("[personal data removed]", s))
            return s
        return obj


# Trace detail fields that carry content, by event kind.
CONTENT_FIELDS = {"raw", "task", "arguments", "comment", "proposed", "output", "show", "question", "answer", "text"}


def _costs() -> dict[str, dict[str, float]]:
    """Per-1,000-token prices by logical model, from NEXA_MODEL_COSTS
    (JSON: {"nexa-general": {"prompt": 0.0025, "completion": 0.01}}). Models
    not listed have no per-token charge configured (local models: none)."""
    try:
        return json.loads(os.environ.get("NEXA_MODEL_COSTS") or "{}")
    except ValueError:
        return {}


def _event_detail(kind: str, d: dict[str, Any], v: View) -> dict[str, Any]:
    out = {}
    for k, x in d.items():
        if k in CONTENT_FIELDS:
            out[k] = v.content(x)
        elif kind == "route.decision" and k == "tested":
            out[k] = [{"field": t.get("field"), "value": v.content(t.get("value"))} for t in x or []]
        elif k in ("by", "initiator"):
            out[k] = v.who(x)
        else:
            out[k] = x
    return out


def build(run_id: str, *, view: View, tool_audit: Optional[list[dict[str, Any]]] = None,
          tool_audit_error: Optional[str] = None) -> Optional[dict[str, Any]]:
    from ..messaging import bus
    from ..orchestration import engine
    from ..policy import pdp
    from ..state import store as state

    run = engine.load_run(run_id)
    if run is None:
        return None
    trace = engine.trace_of(run_id)
    conv = bus.conversation(run_id)
    decisions = sorted(pdp.decisions(run_id=run_id, limit=2000), key=lambda d: d["ts"])
    access = sorted(state.access_log(target=run_id, limit=2000), key=lambda a: a["seq"])
    sid = (run.ctx.get("_session") or {}).get("id")
    if sid:
        access += [a for a in sorted(state.access_log(target=sid, limit=2000), key=lambda a: a["seq"])
                   if a.get("via") == run.workflow_id or a["action"] in ("create",)]
    audit_by_call = {a["call_id"]: a for a in (tool_audit or [])}
    costs = _costs()

    # ---------------------------------------------------------- steps
    steps: list[dict[str, Any]] = []
    open_by_name: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    started = trace[0]["ts"] if trace else run.created_at
    ended = trace[-1]["ts"] if trace else run.created_at
    for t in trace:
        k, name = t["kind"], t["step"]
        if k == "step.started":
            sp = {"span_id": f"step:{t['seq']}", "kind": "step", "step": name, "type": t["detail"].get("type"),
                  "title": t["detail"].get("title"), "start": t["ts"], "end": None, "status": "running",
                  "children": [], "events": []}
            steps.append(sp)
            open_by_name[name] = sp
            continue
        sp = open_by_name.get(name)
        if k in ("step.completed", "step.failed") and sp:
            sp["end"], sp["status"] = t["ts"], "ok" if k == "step.completed" else "failed"
            sp["latency_ms"] = t["detail"].get("latency_ms")
            if k == "step.failed":
                sp["error"] = t["detail"].get("error")
            open_by_name.pop(name, None)
        if k == "checkpoint.waiting" and sp:
            sp["status"] = "waiting"
        if k == "run.interrupted":
            for o in open_by_name.values():
                o["end"], o["status"] = t["ts"], "interrupted"
            open_by_name.clear()
        if k in ("agent.call", "tool.call", "knowledge.call", "step.completed", "step.started"):
            continue        # represented as spans
        ev = {"seq": t["seq"], "ts": t["ts"], "kind": k, "step": name, "detail": _event_detail(k, t["detail"], view)}
        (sp["events"] if sp and k not in ("run.started",) else events).append(ev)

    def parent_for(step_name: Optional[str], ts: float) -> Optional[dict[str, Any]]:
        cands = [s for s in steps if (step_name is None or s["step"] == step_name) and s["start"] <= ts + 0.001]
        return cands[-1] if cands else None

    # ---------------------------------------------------------- calls (messages)
    msgs = [row["message"] | {"_status": row["status"], "_seq": row["seq"]} for row in conv]
    by_id = {m["message_id"]: m for m in msgs}
    replies = {}
    for m in msgs:
        if m["kind"] in ("result", "error") and m.get("in_reply_to"):
            replies.setdefault(m["in_reply_to"], m)
    calls: list[dict[str, Any]] = []
    for m in msgs:
        if m["kind"] != "request":
            continue
        p = m.get("payload") or {}
        rep = replies.get(m["message_id"])
        rp = (rep or {}).get("payload") or {}
        sp = {"span_id": m["message_id"], "kind": "agent_call" if m["type"] == "agent.task" else "tool_call",
              "start": m["created_at"], "end": rep["created_at"] if rep else None,
              "latency_ms": int((rep["created_at"] - m["created_at"]) * 1000) if rep else None,
              "attempt": m.get("attempt"), "max_attempts": m.get("max_attempts"),
              "deadline_s": round(m["deadline"] - m["created_at"], 1) if m.get("deadline") else None,
              "idempotency": "duplicate" if m["_status"] == "duplicate" else m["_status"],
              "permission": m.get("permission"), "on_behalf_of": view.who((m.get("principal") or {}).get("subject")),
              "causation": m.get("causation_id"), "reply": rep["message_id"] if rep else None,
              "status": "ok" if rep and rep["kind"] == "result" else ("error" if rep else "no reply")}
        if rep and rep["kind"] == "error":
            sp["error"] = {k: rep["error"].get(k) for k in ("code", "retryable", "status", "reason")}
        if m["type"] == "agent.task":
            model = rp.get("model")
            pt, ct = rp.get("prompt_tokens"), rp.get("completion_tokens")
            price = costs.get(model or "")
            sp.update(agent=m["receiver"]["id"], agent_version=rp.get("agent_version") or m["receiver"].get("version"),
                      role=p.get("role"), manifest=rp.get("manifest"),
                      model={"model": model, "fallback_model": rp.get("fallback_model"), "attempts": rp.get("attempts"),
                             "prompt_tokens": pt, "completion_tokens": ct,
                             "cost": round((pt or 0) / 1000 * price.get("prompt", 0) +
                                           (ct or 0) / 1000 * price.get("completion", 0), 6) if price else None},
                      prompt=view.content(p.get("prompt")), output=view.content(rp.get("output")),
                      redacted_items=rp.get("redacted"))
            step_name = p.get("step")
        else:
            result = rp.get("result") or {}
            call_id = rp.get("call_id") or (CALL_ID.search((rep or {}).get("error", {}).get("reason", "") or "") or
                                            [None, None])[1] if rep else None
            gw = audit_by_call.get(call_id or "")
            sp.update(tool=p.get("tool"), tool_version=rp.get("tool_version") or m["receiver"].get("version"),
                      for_agent=p.get("for_agent"), scopes=rp.get("scopes") or p.get("scopes"), call_id=call_id,
                      arguments=view.content(p.get("arguments")),
                      result=view.content({k: v for k, v in result.items() if k not in ("citations",)}) if result
                      else None,
                      retrieval={"status": result.get("status"), "confidence": result.get("confidence"),
                                 "evidence": result.get("evidence"),
                                 "references": [{k: c.get(k) for k in ("marker", "title", "page", "chunk_id", "used")}
                                                for c in result.get("citations") or []]}
                      if "citations" in result or "status" in result else None,
                      gateway={k: gw.get(k) for k in ("decision", "check", "reason", "status", "latency_ms", "kind",
                                                      "connector", "output_bytes", "args_digest")} if gw else None)
            step_name = p.get("step")
        calls.append(sp)
        par = parent_for(step_name, m["created_at"])
        (par["children"] if par else events).append(sp)

    # ---------------------------------------------------------- handoffs
    handoffs = []
    for m in msgs:
        if m["type"] != "delegation.choice":
            continue
        p = m.get("payload") or {}
        coord = by_id.get(m.get("causation_id") or "")
        nxt = next((x for x in msgs if x.get("causation_id") == m["message_id"] and x["kind"] == "request"), None)
        handoffs.append({"decision_event": m["message_id"], "step": p.get("step"), "decision": p.get("decision"),
                         "from": (coord or {}).get("sender", {}).get("id"), "from_result": (coord or {}).get("message_id"),
                         "proposed": view.content(p.get("proposed")), "accepted": p.get("accepted"),
                         "action": p.get("action"), "to": (nxt or {}).get("receiver", {}).get("id"),
                         "to_request": (nxt or {}).get("message_id"), "problem": p.get("problem")})

    # ---------------------------------------------------------- policy and state
    pol = []
    for d in decisions:
        rec = {"decision_id": d["decision_id"], "ts": d["ts"], "action": d["action"], "decision": d["decision"],
               "rule": d["rule"], "reason": d["reason"], "agent": d.get("agent"),
               "agent_version": d.get("agent_version"), "resource": d["resource"], "actor": view.who(d["actor"]),
               "roles": d.get("roles"), "tenant": d.get("tenant")}
        pol.append(rec)
        par = parent_for(None, d["ts"])
        if par:
            par["events"].append({"ts": d["ts"], "kind": f"policy.{d['action']}", "detail": rec})
    acc = [{"ts": a["ts"], "actor": view.who(a["actor"]), "action": a["action"], "kind": a["kind"],
            "target": a["target"] if a["kind"] == "run" else "session", "version": a.get("version"),
            "keys": a.get("keys"), "via": a.get("via"), "decision": a["decision"], "reason": a.get("reason")}
           for a in access]

    # ---------------------------------------------------------- summary
    agent_calls = [c for c in calls if c["kind"] == "agent_call"]
    tool_calls = [c for c in calls if c["kind"] == "tool_call"]
    tokens: dict[str, dict[str, int]] = {}
    cost = 0.0
    for c in agent_calls:
        md = c["model"]
        if md.get("model"):
            t = tokens.setdefault(md["model"], {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            t["calls"] += 1
            t["prompt_tokens"] += md.get("prompt_tokens") or 0
            t["completion_tokens"] += md.get("completion_tokens") or 0
        cost += md.get("cost") or 0.0
    summary = {
        "status": run.status, "workflow": run.workflow_id, "workflow_version": run.workflow_version,
        "initiator": view.who(run.principal.subject), "roles": list(run.principal.roles),
        "school": getattr(run.principal, "school_id", None),
        "duration_s": round(ended - started, 2), "running_s": round(run.counters.get("running_s", 0.0), 2),
        "steps": len(steps), "agent_calls": len(agent_calls),
        "agents": sorted({c["agent"] for c in agent_calls}),
        "model_usage": tokens, "cost": round(cost, 6) if any(c["model"].get("cost") is not None for c in agent_calls)
        else "not configured (local models have no per-token charge)",
        "tool_calls": len(tool_calls), "tools": sorted({c["tool"] for c in tool_calls}),
        "retrieval_references": sum(len((c.get("retrieval") or {}).get("references") or []) for c in tool_calls),
        "handoffs": len(handoffs), "retries": sum(1 for c in calls if (c.get("attempt") or 1) > 1),
        "answered_from_idempotency_record": sum(1 for c in calls if c["idempotency"] == "duplicate"),
        "errors": [{"span": c["span_id"], "kind": c["kind"], **(c.get("error") or {})} for c in calls if c.get("error")]
        + [{"step": s["step"], "error": s.get("error")} for s in steps if s["status"] == "failed"],
        "policy": {"allow": sum(d["decision"] == "allow" for d in pol), "deny": sum(d["decision"] == "deny" for d in pol)},
        "messages": len(msgs), "trace_entries": len(trace),
        "tool_gateway_audit": "joined" if tool_audit is not None else f"unavailable: {tool_audit_error}",
    }
    rec = {
        "schema": TRACE_SCHEMA, "view": view.kind, "run_id": run.run_id, "trace_id": run.trace_id,
        "correlation_id": run.run_id, "summary": summary,
        "run": {"span_id": f"run:{run.run_id}", "start": started, "end": ended, "status": run.status,
                "input": view.content(run.input), "output": view.content(run.output), "error": run.error,
                "children": steps, "events": events},
        "handoffs": handoffs, "policy_decisions": pol, "state_access": acc,
    }
    return view.scrub(rec)


def render_text(rec: dict[str, Any]) -> str:
    """A readable tree for operators (v1/tools/trace_view.py)."""
    s = rec["summary"]
    t0 = rec["run"]["start"]
    out = [f"run {rec['run_id']}  trace {rec['trace_id']}  [{rec['view']} view]",
           f"  {s['workflow']} {s['workflow_version']}  status {s['status']}  {s['duration_s']} s "
           f"({s['running_s']} s running)  started by {s['initiator']} {s['roles']}",
           f"  {s['steps']} steps, {s['agent_calls']} agent calls ({', '.join(s['agents']) or '-'}), "
           f"{s['tool_calls']} tool calls ({', '.join(s['tools']) or '-'}), {s['retrieval_references']} retrieval "
           f"references, {s['handoffs']} handoffs, {s['retries']} retries, "
           f"{s['answered_from_idempotency_record']} answered from the idempotency record",
           "  model usage: " + (", ".join(f"{m} {u['calls']} calls {u['prompt_tokens']}+{u['completion_tokens']} tokens"
                                           for m, u in s["model_usage"].items()) or "-") + f"; cost: {s['cost']}",
           f"  policy: {s['policy']['allow']} allowed, {s['policy']['deny']} denied; errors: {len(s['errors'])}; "
           f"tool gateway audit: {s['tool_gateway_audit']}", ""]

    def at(ts):
        return f"+{ts - t0:7.2f}s"
    for e in rec["run"]["events"]:
        if e["kind"] == "run.started":
            out.append(f"{at(e['ts'])}  run.started")
    for st in rec["run"]["children"]:
        out.append(f"{at(st['start'])}  step {st['step']} ({st['type']})  {st['status']}"
                   + (f"  {st.get('latency_ms')} ms" if st.get("latency_ms") is not None else ""))
        items = [("call", c["start"], c) for c in st["children"]] + [("event", e["ts"], e) for e in st["events"]]
        for kind, ts, x in sorted(items, key=lambda i: i[1]):
            if kind == "call" and x["kind"] == "agent_call":
                md = x["model"]
                out.append(f"{at(ts)}    agent {x['role']:<11} {x['agent']} {x['agent_version'] or ''}  {x['status']}"
                           f"  {x['latency_ms']} ms  model {md.get('model')}  tokens {md.get('prompt_tokens')}+"
                           f"{md.get('completion_tokens')}  attempt {x['attempt']}  [{x['idempotency']}]"
                           + (f"  ERROR {x['error']['code']}: {x['error']['reason'][:60]}" if x.get("error") else ""))
            elif kind == "call":
                r = x.get("retrieval") or {}
                g = x.get("gateway") or {}
                out.append(f"{at(ts)}    tool  {x['tool']} {x['tool_version'] or ''} for {x['for_agent']}  {x['status']}"
                           f"  {x['latency_ms']} ms  scopes {x['scopes']}  call {x['call_id']}  gateway "
                           f"{g.get('decision')}/{g.get('status')}"
                           + (f"  retrieval {r.get('status')} {len(r.get('references') or [])} refs" if r else ""))
                for ref in (r.get("references") or [])[:6]:
                    out.append(f"{'':>10}      ref {ref.get('marker')}  {ref.get('title')}  p.{ref.get('page')}  "
                               f"{ref.get('chunk_id')}")
            else:
                d = x["detail"]
                if x["kind"].startswith("policy."):
                    out.append(f"{at(ts)}    policy {d['action']} {d['decision']} rule={d['rule']}  {d['resource']}")
                elif x["kind"] == "route.decision":
                    out.append(f"{at(ts)}    route → {d.get('goto')} ({d.get('reason')})")
                elif x["kind"] == "delegate.choice":
                    out.append(f"{at(ts)}    coordinator proposed {d.get('action')!r}: "
                               f"{'accepted' if d.get('accepted') else 'refused ' + str(d.get('problem'))}")
                elif x["kind"].startswith("checkpoint."):
                    out.append(f"{at(ts)}    {x['kind']} {d.get('decision') or ''} {d.get('by') or ''}")
                elif x["kind"] in ("step.attempt_failed", "step.error_path", "agent.fallback", "session.written",
                                   "session.conflict", "delegate.finished"):
                    out.append(f"{at(ts)}    {x['kind']} " + json.dumps({k: v for k, v in d.items()
                                                                          if not isinstance(v, dict)})[:100])
    for e in rec["run"]["events"]:
        if e["kind"].startswith("run.") and e["kind"] != "run.started":
            out.append(f"{at(e['ts'])}  {e['kind']}")
    if rec["handoffs"]:
        out.append("\nhandoffs (coordinator result → orchestrator decision → delegate request):")
        for hnd in rec["handoffs"]:
            out.append(f"  {hnd['from']} → {'accepted' if hnd['accepted'] else 'refused'} {hnd['action']!r} → "
                       f"{hnd['to'] or '-'}   ({hnd['from_result']} → {hnd['decision_event']} → {hnd['to_request']})")
    return "\n".join(out)
