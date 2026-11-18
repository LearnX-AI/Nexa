"""Live check of agent state, sessions and safe resume (NEXA-M3-006).

On a running NEXA, through the API Gateway:

  1. Session handoff: a teacher creates a session for the lesson explainer,
     runs it, a second teacher approves; the session now holds the topic;
     a second run with the same session is told the earlier topic.
  2. Policy: another teacher cannot see the session; an admin sees its
     metadata only; a learner record (marks) is refused; an auditor reads the
     access log of every read and write.
  3. Safe resume: a multi-agent run is started and the Agent Runtime is
     restarted in the middle of it (sudo systemctl restart nexa-agent-runtime,
     unless --no-restart). The run comes back interrupted and resumable; the
     teacher resumes it; work finished before the restart is not redone
     (its requests are answered from the idempotency record); the run
     completes; every checkpoint is valid.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     python -m v1.tools.state_check)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import httpx

from v1.identity.tokens import issue_token

WF = "nexa-wf-lesson-explainer"


def hdr(sub, roles):
    return {"Authorization": "Bearer " + issue_token(sub, roles, "user", 3600)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--runtime", default="http://127.0.0.1:8102", help="Agent Runtime (health)")
    ap.add_argument("--no-restart", action="store_true", help="skip the restart-and-resume check")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=60)
    t1, t2 = hdr("check:state-teacher-1", ["teacher"]), hdr("check:state-teacher-2", ["teacher"])
    admin, auditor = hdr("check:state-admin", ["admin"]), hdr("check:state-auditor", ["auditor"])
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    def wait(run_id, h, until=("waiting_approval", "succeeded", "failed", "rejected", "budget_exceeded",
                               "interrupted"), timeout=600):
        t0, run = time.time(), {}
        while time.time() - t0 < timeout:
            try:
                run = c.get(f"/v1/workflow-runs/{run_id}?trace=false", headers=h).json()
            except httpx.HTTPError:
                time.sleep(2)
                continue
            if run.get("status") in until:
                return run
            time.sleep(2)
        return run

    # 1. session handoff
    s = c.post("/v1/sessions", json={"workflows": [WF], "ttl_s": 3600}, headers=t1)
    check("session created", s.status_code == 201, f"{s.status_code} {s.json().get('id')} v{s.json().get('version')}")
    sid = s.json().get("id")
    r1 = c.post(f"/v1/workflows/{WF}/runs?wait=false", json={"input": {"topic": "Simple interest", "grade": "11"},
                                                             "session_id": sid}, headers=t1).json()
    run1 = wait(r1["run_id"], t1)
    if run1.get("status") == "waiting_approval":
        c.post(f"/v1/workflow-runs/{r1['run_id']}/decision", json={"decision": "approve"}, headers=t2)
        run1 = wait(r1["run_id"], t1, until=("succeeded", "failed"))
    rec = c.get(f"/v1/sessions/{sid}", headers=t1).json().get("record") or {}
    check("run 1 hands the topic to the session", run1.get("status") == "succeeded" and
          (rec.get("data") or {}).get("last_topic") == "Simple interest",
          f"{run1.get('status')}; session v{rec.get('version')} {rec.get('data')}")
    r2 = c.post(f"/v1/workflows/{WF}/runs?wait=false", json={"input": {"topic": "Compound interest", "grade": "11"},
                                                             "session_id": sid}, headers=t1).json()
    run2 = wait(r2["run_id"], t1)
    msgs = c.get(f"/v1/workflow-runs/{r2['run_id']}/messages", headers=t1).json()["messages"]
    first = next((m["message"] for m in msgs if m["message"]["kind"] == "request"), {})
    prompt = (first.get("payload") or {}).get("prompt", "")
    check("run 2 sees the earlier topic", 'topic "Simple interest"' in prompt,
          prompt[prompt.find("Earlier"):prompt.find("Earlier") + 90] if "Earlier" in prompt else run2.get("status"))
    if run2.get("status") == "waiting_approval":
        c.post(f"/v1/workflow-runs/{r2['run_id']}/decision", json={"decision": "reject", "comment": "check"},
               headers=t2)

    # 2. policy and audit
    check("another teacher cannot see the session", c.get(f"/v1/sessions/{sid}", headers=t2).status_code == 404)
    am = c.get(f"/v1/sessions/{sid}", headers=admin).json()
    check("an admin sees metadata only", "metadata" in am and "record" not in am,
          f"keys {am.get('metadata', {}).get('keys')}")
    w = c.put(f"/v1/sessions/{sid}/state", json={"data": {"marks": 72}, "expected_version": rec.get("version", 1)},
              headers=t1)
    check("a learner record is refused (422)", w.status_code == 422, str(w.json().get("detail"))[:120])
    log = c.get("/v1/state/access", params={"target": sid}, headers=auditor).json().get("access", [])
    acts = sorted({(x["action"], x["decision"]) for x in log})
    check("every session access audited", {("create", "allow"), ("read", "allow"), ("write", "allow"),
                                           ("metadata", "deny"), ("write", "deny")} <= set(acts), str(acts))

    # 3. restart and resume
    if not a.no_restart:
        r = c.post(f"/v1/workflows/{WF}/runs?wait=false", json={"input": {"topic": "Percentages", "grade": "11"}},
                   headers=t1).json()
        rid = r["run_id"]
        t0 = time.time()
        while time.time() - t0 < 300:
            tr = c.get(f"/v1/workflow-runs/{rid}", headers=t1).json().get("trace", [])
            if sum(1 for x in tr if x["kind"] == "agent.call" and x["detail"].get("ok")) >= 1:
                break
            time.sleep(0.5)
        done_before = sum(1 for x in tr if x["kind"] == "agent.call" and x["detail"].get("ok"))
        p = subprocess.run(["sudo", "-n", "systemctl", "restart", "nexa-agent-runtime"], capture_output=True, text=True)
        check("Agent Runtime restarted mid-run", p.returncode == 0,
              f"after {done_before} finished agent call(s)" if p.returncode == 0 else p.stderr.strip()[:150])
        for _ in range(60):
            try:
                if httpx.get(a.runtime.rstrip("/") + "/health", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        run = c.get(f"/v1/workflow-runs/{rid}?trace=false", headers=t1).json()
        check("run is interrupted and resumable", run.get("status") == "interrupted" and run.get("resumable"),
              f"{run.get('status')} resumable={run.get('resumable')} {run.get('not_resumable_because') or ''}")
        other = c.post(f"/v1/workflow-runs/{rid}/resume?wait=false", headers=t2)
        check("only its initiator may resume it", other.status_code == 404, str(other.status_code))
        res = c.post(f"/v1/workflow-runs/{rid}/resume?wait=false", headers=t1)
        run = wait(rid, t1, until=("waiting_approval", "succeeded", "failed", "budget_exceeded"))
        conv = c.get(f"/v1/workflow-runs/{rid}/messages", headers=t1).json()
        dup = sum(1 for m in conv["messages"] if m["message"]["kind"] == "result" and m["status"] == "duplicate")
        check("resumed from the checkpoint", res.status_code == 200 and run.get("status") == "waiting_approval",
              f"{res.status_code} -> {run.get('status')} {run.get('error') or ''}")
        check("finished work not redone", dup >= done_before >= 1,
              f"{dup} request(s) answered from the idempotency record")
        check("conversation still valid", conv["check"]["valid"], "; ".join(conv["check"]["problems"][:2]))
        if run.get("status") == "waiting_approval":
            c.post(f"/v1/workflow-runs/{rid}/decision", json={"decision": "approve"}, headers=t2)
            run = wait(rid, t1, until=("succeeded", "failed"))
        cps = c.get(f"/v1/workflow-runs/{rid}/checkpoints", headers=t1).json()["checkpoints"]
        check("run completes; every checkpoint valid", run.get("status") == "succeeded" and cps and
              all(x["valid"] for x in cps), f"{run.get('status')}; checkpoints v{[x['version'] for x in cps]}")
        print(f"\nResumed run {rid}:")
        for x in c.get(f"/v1/workflow-runs/{rid}", headers=t1).json()["trace"]:
            if x["kind"] in ("run.started", "agent.call", "run.interrupted", "run.resumed", "checkpoint.decided",
                             "run.succeeded", "delegate.choice"):
                d = x["detail"]
                info = {k: d[k] for k in ("role", "agent", "latency_ms", "action", "checkpoint", "decision")
                        if d.get(k) is not None}
                print(f"  {x['seq']:>3} {x['step'] or '-':<8} {x['kind']:<18} {info}")
    c.delete(f"/v1/sessions/{sid}", headers=t1)

    print()
    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {det[:150]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
