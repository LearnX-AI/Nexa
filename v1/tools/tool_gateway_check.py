"""Live check of the Tool & Connector Gateway (NEXA-M3-004).

As a real student, through the API Gateway, with nexa-curriculum-tutor 0.2.0
(it must be active: see v1/tools/agent_lifecycle.py):

  1. knowledge_answer (connector: the Knowledge Service, the student's own session)
  2. calculator (built in, exact arithmetic)
  3. both in one workflow run (nexa-wf-curriculum-tutor)

and refusals, each audited: a tool the agent is not granted, a call that does
not come from the Agent Runtime, arguments outside the tool's schema. Then
prints the gateway's audit record of every call.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     python -m v1.tools.tool_gateway_check)
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx

from v1.identity.tokens import issue_token

AGENT = "nexa-curriculum-tutor"
FIELDS = ("call_id", "trace_id", "run_id", "step", "user", "roles", "agent", "agent_version", "tool", "tool_version",
          "kind", "connector", "scopes", "args_digest", "args_preview", "decision", "check", "reason", "status",
          "latency_ms", "output_bytes", "output_digest")


def tok(sub, roles):
    return issue_token(sub, roles, "user", 3600)


def call_id(body) -> str | None:
    if not isinstance(body, dict):
        return None
    d = body.get("detail")
    return body.get("call_id") or (d.get("call_id") if isinstance(d, dict) else None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--tools", default="http://127.0.0.1:8105", help="Tool Gateway (health and audit)")
    ap.add_argument("--question", default="What is photosynthesis?")
    ap.add_argument("--calculation", default="Calculate the simple interest on K500 at 4% per year for 3 years.")
    a = ap.parse_args(argv)
    student_tok = tok("check:tools-student", ["student"])
    student = {"Authorization": f"Bearer {student_tok}"}
    auditor = {"Authorization": f"Bearer {tok('check:tools-auditor', ['auditor'])}"}
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=240)
    g = httpx.Client(base_url=a.tools.rstrip("/"), timeout=30)
    results: list[tuple[str, bool, str]] = []
    calls: list[str] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    h = g.get("/health").json()
    check("tool gateway up", h.get("status") == "ok",
          ", ".join(f"{k} {v['version']} {v['state']} ({v['implementation']})" for k, v in h["tools"].items()))
    ag = c.get(f"/v1/registry/agents/{AGENT}", headers=student).json()
    check(f"{AGENT} is active", ag.get("state") == "active" and ag.get("enabled"),
          f"{ag.get('version')} {ag.get('state')}")
    if ag.get("state") != "active":
        print(f"{AGENT} is not active: approve and activate it first (python -m v1.tools.agent_lifecycle --help)")

    # 1. knowledge_answer
    r = c.post(f"/v1/agents/{AGENT}/tools/knowledge_answer", json={"arguments": {"question": a.question}},
               headers=student)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    res = body.get("result") or {}
    check("knowledge_answer through the gateway", r.status_code == 200,
          f"{r.status_code} status={res.get('status')} citations={len(res.get('citations') or [])} "
          f"call={call_id(body)} {'' if r.status_code == 200 else body.get('detail')}")
    if call_id(body):
        calls.append(call_id(body))

    # 2. calculator
    r = c.post(f"/v1/agents/{AGENT}/tools/calculator", json={"arguments": {"expression": "500*4%*3"}},
               headers=student)
    body = r.json()
    check("calculator through the gateway", r.status_code == 200 and (body.get("result") or {}).get("exact") == "60",
          f"{r.status_code} {(body.get('result') or {}).get('text')} call={body.get('call_id')}")
    if call_id(body):
        calls.append(call_id(body))

    # 3. refusals
    r = c.post("/v1/agents/nexa-tutor/tools/calculator", json={"arguments": {"expression": "1+1"}}, headers=student)
    check("agent not granted the tool: 403 (runtime)", r.status_code == 403, f"{r.status_code}")
    r = g.post("/v1/tools/calculator/invoke", json={"arguments": {"expression": "1+1"}},
               headers=student | {"X-NEXA-On-Behalf-Of": f"Bearer {student_tok}", "X-NEXA-Agent": AGENT})
    check("not from the Agent Runtime: 403 (gateway)", r.status_code == 403, f"{r.status_code} {r.json()}")
    if call_id(r.json()):
        calls.append(call_id(r.json()))
    r = c.post(f"/v1/agents/{AGENT}/tools/calculator", json={"arguments": {"expression": "__import__('os')"}},
               headers=student)
    detail = r.json().get("detail") or {}
    check("arguments outside the schema: 422 (gateway)", r.status_code == 422, f"{r.status_code} {detail}")
    if call_id(r.json()):
        calls.append(call_id(r.json()))

    # 4. a workflow that uses both
    r = c.post("/v1/workflows/nexa-wf-curriculum-tutor/runs?wait=false", json={"input": {"question": a.calculation}},
               headers=student)
    run = {}
    if r.status_code == 202:
        rid = r.json()["run_id"]
        for _ in range(120):
            run = c.get(f"/v1/workflow-runs/{rid}", headers=student).json()
            if run["status"] not in ("running",):
                break
            time.sleep(3)
    tool_steps = [t["detail"] for t in run.get("trace", []) if t["kind"] == "tool.call"]
    check("workflow with a tool step succeeds", run.get("status") == "succeeded",
          f"{run.get('status')} " + ", ".join(f"{d['tool']}={'ok' if d['ok'] else d.get('status')}" for d in tool_steps))
    if run.get("output"):
        print("\nWorkflow answer:\n  " + run["output"][:900].replace("\n", "\n  "))

    # 5. the audit trail
    rows = [g.get("/v1/tools/audit", params={"call_id": cid}, headers=auditor).json()["calls"] for cid in calls]
    rows = [x[0] for x in rows if x]
    if run.get("run_id"):
        rows += g.get("/v1/tools/audit", params={"run_id": run["run_id"]}, headers=auditor).json()["calls"]
    complete = [x for x in rows if all(k in x for k in FIELDS) and x["user"] and x["agent"] and x["tool"]
                and x["decision"] and x["status"]]
    check("every call audited, every record complete", rows and len(complete) == len(rows) == len(calls) +
          len([x for x in rows if x.get("run_id")]), f"{len(complete)}/{len(rows)} records")
    print("\nAudit trail (Tool Gateway):")
    for x in rows:
        print(f"  {x['call_id']}  {x['decision']:<5} {x['status']}  {x['tool']:<16} agent={x['agent']} "
              f"{x['agent_version'] or ''}  user={x['user']}  scopes={','.join(x['scopes'])}  "
              f"check={x['check'] or '-'}  {x['latency_ms']} ms  run={x['run_id'] or '-'} step={x['step'] or '-'}  "
              f"trace={(x['trace_id'] or '')[:12]}")

    print()
    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {det[:150]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
