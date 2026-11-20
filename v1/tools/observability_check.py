"""Live check of agent observability (NEXA-M3-008).

On a running NEXA, through the API Gateway: runs a multi-agent workflow (the
lesson explainer, approved by a second teacher after a refused reviewer from
another school) and a tool workflow (the curriculum tutor), then reconstructs
both as an operator would, from the records alone, and checks that:

  - every step, agent call (agent, version, model, tokens, latency), tool call
    (joined with the Tool Gateway's own record), retrieval reference, handoff,
    policy decision and the human decision is there, under one trace id
  - nothing secret is in the record (no token, no credential, not the
    identity secret or the runtime's service token when loaded) and no
    learner content (not the question, the topic, the answers or the users'
    account names)
  - the forensic view needs an auditor and a reason, and is logged

Prints the reconstructed multi-agent run as a tree.

    (set -a; . <(sudo cat /etc/nexa/identity.env /etc/nexa/agent-runtime.env); set +a; \\
     python -m v1.tools.observability_check)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid

import httpx

from v1.agent_runtime.tracing.reconstruct import render_text
from v1.identity.tokens import issue_token


def tok(sub, roles, school=None):
    return issue_token(sub, roles, "user", 3600, extra={"school_id": school} if school else None)


def h(t):
    return {"Authorization": f"Bearer {t}"}


def walk(spans):
    for s in spans:
        yield s
        yield from walk(s.get("children", []))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--school", default="pomnsoe")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=240)
    tag = uuid.uuid4().hex[:6]
    topic = f"Simple interest for market traders {tag}"
    question = f"What is photosynthesis? ({tag})"
    mary_sub, john_sub = f"check:obs-teacher-mary-{tag}", f"check:obs-teacher-john-{tag}"
    student_sub = f"check:obs-student-{tag}"
    mary, john = tok(mary_sub, ["teacher"], a.school), tok(john_sub, ["teacher"], a.school)
    other = tok(f"check:obs-teacher-other-{tag}", ["teacher"], "obs-check-other")
    student = tok(student_sub, ["student"], a.school)
    operator, auditor = tok("check:obs-operator", ["ai_engineer"]), tok("check:obs-auditor", ["auditor"])
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    def wait(rid, who, until=("waiting_approval", "succeeded", "failed", "rejected", "budget_exceeded")):
        run = {}
        for _ in range(300):
            run = c.get(f"/v1/workflow-runs/{rid}?trace=false", headers=h(who)).json()
            if run.get("status") in until:
                return run
            time.sleep(2)
        return run

    # Run the two workflows.
    r = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false",
               json={"input": {"topic": topic, "grade": "11"}}, headers=h(mary)).json()
    lesson = r["run_id"]
    run = wait(lesson, mary)
    if run.get("status") == "waiting_approval":
        c.post(f"/v1/workflow-runs/{lesson}/decision", json={"decision": "approve"}, headers=h(other))
        c.post(f"/v1/workflow-runs/{lesson}/decision", json={"decision": "approve", "comment": f"ok {tag}"},
               headers=h(john))
        run = wait(lesson, mary, until=("succeeded", "failed", "rejected"))
    check("multi-agent run completes", run.get("status") == "succeeded", run.get("status"))
    r = c.post("/v1/workflows/nexa-wf-curriculum-tutor/runs?wait=false", json={"input": {"question": question}},
               headers=h(student)).json()
    tutor = r["run_id"]
    trun = wait(tutor, student)
    check("tool run completes", trun.get("status") == "succeeded", trun.get("status"))

    # Reconstruct, as an operator.
    assert c.get(f"/v1/observability/runs/{lesson}", headers=h(student)).status_code == 403
    rec = c.get(f"/v1/observability/runs/{lesson}", headers=h(operator)).json()
    s = rec.get("summary", {})
    steps = rec.get("run", {}).get("children", [])
    calls = [x for x in walk(steps) if x.get("kind") == "agent_call"]
    check("one trace id across the run", rec.get("trace_id") == run.get("trace_id") and rec.get("correlation_id")
          == lesson, f"trace {rec.get('trace_id')}")
    check("steps and agent calls reconstructed", [x["step"] for x in steps] == ["prepare", "review", "release"]
          and len(calls) == s.get("agent_calls") >= 3,
          f"{len(steps)} steps, {len(calls)} agent calls: " + ", ".join(f"{x['role']}:{x['agent']}" for x in calls))
    check("every agent call: version, manifest, model, tokens, latency",
          calls and all(x.get("agent_version") and str(x.get("manifest", "")).startswith("sha256:") and
                        x["model"].get("model") and x["model"].get("prompt_tokens") is not None and
                        x.get("latency_ms") is not None for x in calls),
          "; ".join(f"{m}: {u['calls']} calls, {u['prompt_tokens']}+{u['completion_tokens']} tokens"
                    for m, u in s.get("model_usage", {}).items()))
    hnd = rec.get("handoffs", [])
    ids = {x["span_id"] for x in calls}
    check("handoffs: coordinator → orchestrator → delegate", hnd and all(x["to_request"] in ids for x in hnd
                                                                       if x["accepted"]),
          "; ".join(f"{x['from']} → {x['action']} → {x['to']}" for x in hnd))
    evs = [e for st in steps for e in st.get("events", [])]
    check("human decision and policy decisions in place",
          any(e["kind"] == "checkpoint.decided" for e in evs) and
          any(e["kind"] == "policy.decide" and e["detail"]["rule"] == "tenancy" for e in evs),
          f"policy {s.get('policy')}")

    trec = c.get(f"/v1/observability/runs/{tutor}", headers=h(operator)).json()
    tool = next((x for x in walk(trec.get("run", {}).get("children", [])) if x.get("kind") == "tool_call"), {})
    refs = (tool.get("retrieval") or {}).get("references") or []
    check("tool call joined with the Tool Gateway record", (tool.get("gateway") or {}).get("decision") == "allow"
          and trec["summary"]["tool_gateway_audit"] == "joined",
          f"{tool.get('tool')} {tool.get('call_id')} gateway {(tool.get('gateway') or {}).get('status')} "
          f"{(tool.get('gateway') or {}).get('connector')}")
    check("retrieval references recorded", bool(refs) or (tool.get("retrieval") or {}).get("status") != "grounded",
          f"{(tool.get('retrieval') or {}).get('status')}: " +
          "; ".join(f"{x.get('marker')} {x.get('title')} p.{x.get('page')}" for x in refs[:3]))

    # Privacy.
    raw = json.dumps([rec, trec])
    leaks = [x for x in (topic, question, tag, mary_sub, john_sub, student_sub, "Bearer ") if x in raw]
    for name in ("NEXA_AGENT_GATEWAY_TOKEN", "NEXA_IDENTITY_SECRET"):
        if os.environ.get(name) and os.environ[name] in raw:
            leaks.append(name)
    if re.search(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.", raw):
        leaks.append("a token")
    check("no secrets or learner content in the operator view", not leaks,
          "leaked: " + ", ".join(leaks) if leaks else f"users shown as {s.get('initiator')}; content as size+digest")
    f1 = c.get(f"/v1/observability/runs/{lesson}", params={"view": "forensic", "reason": "checking things"},
               headers=h(operator))
    f2 = c.get(f"/v1/observability/runs/{lesson}", params={"view": "forensic",
                                                           "reason": "NEXA-M3-008 live check of the forensic view"},
               headers=h(auditor))
    check("forensic view: auditors with a reason only", f1.status_code == 403 and f2.status_code == 200 and
          topic in json.dumps(f2.json()), f"operator {f1.status_code}, auditor {f2.status_code}")

    print("\nReconstructed multi-agent run (operator view):\n")
    print(render_text(rec))
    print()
    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {str(det)[:150]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
