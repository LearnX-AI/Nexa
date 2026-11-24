"""Live check of the orchestration engine (NEXA-M3-003).

Runs the two reviewed workflows on a running NEXA, through the API Gateway, as
real users, and prints each run's trace:

  1. nexa-wf-student-question (deterministic): a student asks two questions,
     each twice. Each run must succeed; the same question must take the same
     path; each routing decision must carry its reason; every agent call must
     carry the agent version, manifest fingerprint and the run's trace id.
  2. nexa-wf-lesson-explainer (bounded multi-agent): a teacher starts it; the
     coordinator delegates within its allowlist and limit; the run waits for a
     human; the teacher who started it may not approve it (four eyes); a
     second teacher approves it and the run completes.

No direct agent-to-agent calls: every agent call in the traces is made by the
engine (coordinator or delegate) and they happen one after another.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     python -m v1.tools.orchestration_check)
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

from v1.identity.tokens import issue_token


def hdr(sub, roles):
    return {"Authorization": "Bearer " + issue_token(sub, roles, "user", 3600)}


def wait_for(c, run_id, h, until=("succeeded", "failed", "rejected", "budget_exceeded", "expired", "interrupted",
                                  "waiting_approval"), timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/v1/workflow-runs/{run_id}", headers=h)
        body = r.json()
        if r.status_code != 200:
            # e.g. a reviewer once the run has left review (M3-007: only the initiator and
            # oversight roles see a finished run). Report it, do not poll for 15 minutes.
            print(f"run {run_id}: GET {r.status_code} {body.get('detail')}", file=sys.stderr, flush=True)
            return {**body, "status": f"unreadable ({r.status_code})", "trace": []}
        if body.get("status") in until:
            return body
        time.sleep(3)
    # Report it as a failed check (with the run's state) instead of stopping the whole check.
    print(f"run {run_id} did not finish in {timeout}s: status {body.get('status')}", file=sys.stderr, flush=True)
    return body


def show_trace(run, width=110):
    print(f"\n  run {run['run_id']}  status {run['status']}  trace_id {run['trace_id']}  "
          f"steps {run['counters']['steps']}  agent calls {run['counters']['agent_calls']}  "
          f"running {run['counters']['running_s']:.1f}s")
    for t in run["trace"]:
        d = t["detail"]
        keys = {"route.decision": ("goto", "reason"), "agent.call": ("role", "agent", "version", "latency_ms", "ok"),
                "knowledge.call": ("status", "confidence", "citations", "latency_ms"),
                "delegate.choice": ("decision", "action", "accepted", "problem"),
                "delegate.finished": ("delegations", "decisions", "reason"),
                "checkpoint.decided": ("decision", "by"), "checkpoint.refused": ("by", "reason"),
                "agent.discovered": ("capability", "chose"), "agent.fallback": ("to", "reason"),
                "step.attempt_failed": ("attempt", "error"), "run.started": ("workflow", "version", "location")}
        info = ", ".join(f"{k}={d.get(k)}" for k in keys.get(t["kind"], ()) if d.get(k) is not None)
        print(f"    {t['seq']:>3} {t['step'] or '-':<18} {t['kind']:<20} {info}"[:width])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--question", action="append",
                    help="student question (default: one calculation, one concept)")
    ap.add_argument("--topic", default="Simple interest")
    ap.add_argument("--grade", default="11")
    ap.add_argument("--show-output", action="store_true")
    a = ap.parse_args(argv)
    questions = a.question or ["Calculate the simple interest on K500 at 4% per year for 3 years.",
                               "What is photosynthesis?"]
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=60)
    student = hdr("check:orch-student", ["student"])
    t1, t2 = hdr("check:orch-teacher-1", ["teacher"]), hdr("check:orch-teacher-2", ["teacher"])
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    wfs = {w["id"]: w for w in c.get("/v1/workflows", headers=student).json().get("workflows", [])}
    check("workflows loaded", {"nexa-wf-student-question", "nexa-wf-lesson-explainer"} <= set(wfs),
          ", ".join(f"{k} {v['version']} {v['pattern']}" for k, v in wfs.items()))

    # 1. Deterministic
    print("\n=== 1. nexa-wf-student-question (deterministic)")
    for q in questions:
        paths, runs = [], []
        for _ in range(2):
            r = c.post("/v1/workflows/nexa-wf-student-question/runs?wait=false", json={"input": {"question": q}},
                       headers=student)
            if r.status_code != 202:
                check(f"start: {q[:40]}", False, f"{r.status_code} {r.text[:200]}")
                break
            run = wait_for(c, r.json()["run_id"], student)
            runs.append(run)
            paths.append([t["step"] for t in run["trace"] if t["kind"] == "step.started"])
        if not runs:
            continue
        print(f"\n  Q: {q}")
        show_trace(runs[0])
        if a.show_output:
            print("\n  output: " + (runs[0]["output"] or "")[:800].replace("\n", "\n          "))
        ok_all = all(x["status"] == "succeeded" for x in runs)
        check(f"succeeds: {q[:45]}", ok_all, " / ".join(x["status"] for x in runs))
        check(f"same path twice: {q[:38]}", len(paths) == 2 and paths[0] == paths[1], " -> ".join(paths[0]))
        tr = [t for x in runs for t in x["trace"]]
        routes = [t["detail"] for t in tr if t["kind"] == "route.decision"]
        check(f"route reasons recorded: {q[:32]}", routes and all(d.get("reason") for d in routes),
              "; ".join(f"{d['goto']}: {d['reason']}" for d in routes[:1]))
        calls = [(t["detail"], x["trace_id"]) for x in runs for t in x["trace"] if t["kind"] == "agent.call"
                 and t["detail"].get("ok")]
        check(f"agent calls traced: {q[:36]}",
              all(d.get("version") and str(d.get("manifest", "")).startswith("sha256:") and d["trace_id"] == tid
                  for d, tid in calls),
              f"{len(calls)} calls" + (f", e.g. {calls[0][0]['agent']} {calls[0][0]['version']}" if calls else
                                       " with output"))

    # 2. Bounded multi-agent
    print("\n=== 2. nexa-wf-lesson-explainer (bounded multi-agent)")
    r = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false",
               json={"input": {"topic": a.topic, "grade": a.grade}}, headers=t1)
    check("teacher starts it", r.status_code == 202, f"{r.status_code}")
    if r.status_code == 202:
        run_id = r.json()["run_id"]
        run = wait_for(c, run_id, t1)
        check("pauses for a human", run["status"] == "waiting_approval", run["status"] + (f" {run['error']}"
                                                                                          if run.get("error") else ""))
        tr = run["trace"]
        choices = [t["detail"] for t in tr if t["kind"] == "delegate.choice"]
        dels = [t["detail"] for t in tr if t["kind"] == "agent.call" and t["detail"].get("role") == "delegate"]
        allowed = set(wfs.get("nexa-wf-lesson-explainer", {}).get("agents", []))
        check("delegation bounded (at most 2)", len(dels) <= 2,
              f"{len(choices)} coordinator decisions, {len(dels)} delegations: "
              + ", ".join(f"{d['action']}={'ok' if d['accepted'] else 'refused'}" for d in choices))
        check("only allowlisted agents ran", {d["agent"] for d in dels} <= allowed,
              ", ".join(sorted({d["agent"] for d in dels})))
        calls = [t for t in tr if t["kind"] == "agent.call"]
        check("every agent call made by the engine", all(t["detail"].get("role") in ("coordinator", "delegate")
                                                         for t in calls),
              " -> ".join(f"{t['detail']['role']}:{t['detail']['agent']}" for t in calls))
        if run["status"] == "waiting_approval":
            r1 = c.post(f"/v1/workflow-runs/{run_id}/decision", json={"decision": "approve"}, headers=t1)
            check("starter cannot approve (four eyes)", r1.status_code == 403, f"{r1.status_code}")
            rs = c.post(f"/v1/workflow-runs/{run_id}/decision", json={"decision": "approve"}, headers=student)
            check("student cannot approve", rs.status_code == 403, f"{rs.status_code}")
            r2 = c.post(f"/v1/workflow-runs/{run_id}/decision?wait=false",
                        json={"decision": "approve", "comment": "NEXA-M3-003 live check"}, headers=t2)
            check("second teacher approves", r2.status_code == 200, f"{r2.status_code}")
            # Poll as the initiator: the approver no longer sees the run once it leaves review.
            run = wait_for(c, run_id, t1, until=("succeeded", "failed", "rejected", "budget_exceeded"))
            check("run completes", run["status"] == "succeeded", run["status"])
        show_trace(run)
        if a.show_output and run.get("output"):
            print("\n  output: " + run["output"][:1500].replace("\n", "\n          "))

    print()
    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {det[:150]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
