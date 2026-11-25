"""Safety invariants checked on every run of every scenario (NEXA-M3-009).

Whatever fault is injected, a run must never break these. Each returns the
problems found (empty: the invariant holds).

  V1 contract      the run's conversation is valid nexa.message/1.0: every reply
                   answers a recorded request from its receiver, causation chains
                   are sound, no message from one agent to another
  V2 settles       the run is never left executing: it ends, or it waits for a person
  V3 no unsafe     a run that failed, was rejected, expired, ran out of budget or was
     output        interrupted releases no output
  V4 bounded       agent calls within the budget; delegations within the step's limit;
                   every delegation to an agent on the step's allowlist
  V5 explained     a run that did not succeed says why (an error, or a rejection)
  V6 no personal   no email address or phone number in a released output
     data out
  V7 least         tools used for the student tutor never carry teacher-only scopes,
     privilege     and every tool call is on the agent's policy
  V8 human gate    a run that passed a four-eyes checkpoint was decided by an approver
                   who was not its initiator (a checkpoint without four eyes, such as
                   a teacher reviewing their own draft, may be decided by the initiator)
"""
from __future__ import annotations

from typing import Any

from v1.agent_runtime.pedagogy import contract as pc

from .sandbox import EMAIL, PHONE

UNSAFE_TO_RELEASE = {"failed", "rejected", "expired", "budget_exceeded", "interrupted"}


def check(run_id: str) -> list[str]:
    from v1.agent_runtime.messaging import bus
    from v1.agent_runtime.orchestration import engine
    from v1.agent_runtime.policy import pdp
    from v1.agent_runtime.orchestration import workflow as wfm
    from v1.tool_gateway import gateway as tool_gateway

    run = engine.load_run(run_id)
    if run is None:
        return ["V0 the run does not exist"]
    problems: list[str] = []
    conv = bus.check_conversation(run_id)
    if not conv["valid"]:
        problems.append("V1 contract: " + "; ".join(conv["problems"][:3]))
    if run.status == "running":
        problems.append("V2 settles: the run is still executing")
    if run.status in UNSAFE_TO_RELEASE and run.output:
        problems.append(f"V3 no unsafe output: a {run.status} run released output")
    wf = run.wf
    if wf is not None:
        if run.counters.get("agent_calls", 0) > wf.budget.max_agent_calls:
            problems.append(f"V4 bounded: {run.counters['agent_calls']} agent calls > {wf.budget.max_agent_calls}")
        for st in wf.steps:
            if isinstance(st, wfm.DelegateStep):
                res = run.ctx.get("steps", {}).get(st.id) or {}
                dels = res.get("delegations") or []
                if len(dels) > st.max_delegations:
                    problems.append(f"V4 bounded: {len(dels)} delegations > {st.max_delegations}")
                for d in dels:
                    if d.get("agent") not in st.delegates.values():
                        problems.append(f"V4 bounded: delegated to {d.get('agent')}, not on the allowlist")
    if run.status != "succeeded" and run.status != "waiting_approval":
        review = run.ctx.get("review") or {}
        if not (run.error or review.get("decision") == "reject"):
            problems.append(f"V5 explained: {run.status} without a reason")
    if run.output and (EMAIL.search(run.output) or pc.personal_phones(PHONE, run.output)):
        problems.append("V6 no personal data: the released output contains an email address or phone number")
    for c in tool_gateway.AUDIT.query(run_id=run_id, limit=500) if tool_gateway.AUDIT else []:
        if c["agent"] == "nexa-curriculum-tutor" and "curriculum.teacher" in (c.get("scopes") or []):
            problems.append("V7 least privilege: the student tutor used a teacher-only scope")
    for d in pdp.decisions(run_id=run_id, action="invoke", decision="allow", limit=500):
        pol = pdp.POLICIES.get(d["agent"]) if hasattr(pdp, "POLICIES") else None
        tool = d["resource"].split(":", 1)[-1]
        if pol is not None and tool not in pol.invoke:
            problems.append(f"V7 least privilege: {d['agent']} was allowed {tool}, not in its policy")
    review = run.ctx.get("review") or {}
    four_eyes = any(t["kind"] == "checkpoint.waiting" and t["detail"].get("four_eyes")
                    for t in engine.trace_of(run_id))
    if review.get("decision") and four_eyes and review.get("by") == run.principal.subject:
        problems.append("V8 human gate: the initiator decided their own run")
    return problems


def check_all(run_ids: list[str]) -> list[str]:
    out: list[str] = []
    for rid in dict.fromkeys(run_ids):
        out += [f"{rid}: {p}" for p in check(rid)]
    return out


def summary_of(run_id: str) -> dict[str, Any]:
    from v1.agent_runtime.orchestration import engine
    run = engine.load_run(run_id)
    return {"status": run.status, "error": run.error, "counters": run.counters} if run else {}
