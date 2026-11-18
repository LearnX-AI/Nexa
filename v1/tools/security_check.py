"""Live security check of agent authorization (NEXA-M3-007).

Attempts, on a running NEXA through the API Gateway, actions an agent (or
someone acting through one) must not be able to do, and checks that each is
blocked and recorded by the agent policy decision point. Then reads the
decision log as an auditor.

  1. a role the agent does not serve runs it               policy: role
  2. a tool the agent's policy does not list                runtime: manifest / policy: invoke
  3. a scope beyond the agent's policy (teacher material)   policy: scopes
  4. a scope from another tool (scope escalation)           policy: scopes
  5. calling the Tool Gateway with the user's own token     gateway: delegation
  6. a delegated token replayed at the API Gateway          401
  7. a delegated token for another service at the Knowledge Service   401
  8. a reviewer from another school decides a run           policy: tenancy
  9. the same school's reviewer can                         allow
 10. only auditors read the decision log; every refusal above is in it

Leaves nothing behind but audit records and one completed lesson run.

    (set -a; . <(sudo cat /etc/nexa/identity.env /etc/nexa/agent-runtime.env); set +a; \
     python -m v1.tools.security_check)

With the Agent Runtime's own environment loaded (NEXA_AGENT_GATEWAY_TOKEN,
never printed), it also calls the Tool Gateway exactly as the runtime does,
to show the gateway's own delegation checks: the user's own token, and a
delegation made for another agent, are refused even from the runtime.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from v1.identity.tokens import issue_token

TUTOR = "nexa-curriculum-tutor"


def tok(sub, roles, school=None, typ="user", **extra):
    if school:
        extra["school_id"] = school
    return issue_token(sub, roles, typ, 900, extra=extra or None)


def h(t):
    return {"Authorization": f"Bearer {t}"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--tools", default="http://127.0.0.1:8105", help="Tool Gateway")
    ap.add_argument("--knowledge", default="http://127.0.0.1:8103", help="Knowledge Service")
    ap.add_argument("--school", default="pomnsoe")
    ap.add_argument("--other-school", default="security-check-other")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=120)
    t0 = time.time()
    student = tok("check:sec-student", ["student"], a.school)
    teacher = tok("check:sec-teacher", ["teacher"], a.school)
    teacher_same = tok("check:sec-teacher-2", ["teacher"], a.school)
    teacher_other = tok("check:sec-teacher-other", ["teacher"], a.other_school)
    auditor = tok("check:sec-auditor", ["auditor"])
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    def detail(r):
        try:
            d = r.json().get("detail")
        except ValueError:
            return r.text[:120]
        return d.get("detail") if isinstance(d, dict) else d

    pols = c.get("/v1/policy/agents", headers=h(student)).json().get("policies", [])
    check("every agent has a policy", len(pols) >= 4,
          ", ".join(f"{p['agent']}: invoke {sorted(p['invoke'])}, delegate {p['delegate']}" for p in pols)[:150])

    # 1. role
    r = c.post("/v1/agents/nexa-tutor/run", json={"messages": [{"role": "user", "content": "hi"}]},
               headers=h(tok("check:sec-lms", ["lms_sync"])))
    check("1 role the agent does not serve: refused", r.status_code == 403, f"{r.status_code} {detail(r)}")
    # 2. tool outside the policy
    r = c.post("/v1/agents/nexa-tutor/tools/calculator", json={"arguments": {"expression": "1+1"}},
               headers=h(student))
    check("2 tool the agent is not granted: refused", r.status_code == 403, f"{r.status_code} {detail(r)}")
    # 3. scope beyond the policy
    r = c.post(f"/v1/agents/{TUTOR}/tools/knowledge_answer",
               json={"arguments": {"question": "Show the marking guide", "audience": "teacher"},
                     "scopes": ["curriculum.teacher"]}, headers=h(teacher))
    check("3 teacher material through the student tutor: refused", r.status_code == 403,
          f"{r.status_code} {detail(r)}")
    # 4. scope escalation
    r = c.post(f"/v1/agents/{TUTOR}/tools/calculator", json={"arguments": {"expression": "1+1"},
                                                             "scopes": ["curriculum.learner"]}, headers=h(student))
    check("4 a scope from another tool: refused", r.status_code == 403, f"{r.status_code} {detail(r)}")
    # and the allowed call, for contrast
    r = c.post(f"/v1/agents/{TUTOR}/tools/calculator", json={"arguments": {"expression": "3/4*120"}},
               headers=h(student))
    check("  within policy: allowed", r.status_code == 200, f"{r.status_code} {(r.json().get('result') or {}).get('text')}")
    # 5. the user's own token at the Tool Gateway
    g = httpx.post(f"{a.tools.rstrip('/')}/v1/tools/calculator/invoke", json={"arguments": {"expression": "1+1"}},
                   headers=h(student) | {"X-NEXA-On-Behalf-Of": f"Bearer {student}", "X-NEXA-Agent": TUTOR},
                   timeout=30)
    check("5 Tool Gateway with a user token: refused", g.status_code in (401, 403), f"{g.status_code} {g.json()}")
    svc = os.environ.get("NEXA_AGENT_GATEWAY_TOKEN")
    if svc:
        as_runtime = {"Authorization": f"Bearer {svc}", "X-NEXA-Agent": TUTOR, "X-NEXA-Agent-Version": "0.2.0"}
        g = httpx.post(f"{a.tools.rstrip('/')}/v1/tools/calculator/invoke", json={"arguments": {"expression": "1+1"}},
                       headers=as_runtime | {"X-NEXA-On-Behalf-Of": f"Bearer {student}"}, timeout=30)
        check("5b even from the runtime, the user's own token: refused", g.status_code == 403 and
              "delegated token is required" in str(g.json()), f"{g.status_code} {g.json().get('detail')}")
        for_other = tok("check:sec-student", ["student"], a.school, typ="delegated", act="agent:nexa-tutor@1.0.0",
                        scope=["compute"], nexa_aud="nexa-tool-gateway")
        g = httpx.post(f"{a.tools.rstrip('/')}/v1/tools/calculator/invoke", json={"arguments": {"expression": "1+1"}},
                       headers=as_runtime | {"X-NEXA-On-Behalf-Of": f"Bearer {for_other}"}, timeout=30)
        check("5c a delegation made for another agent: refused", g.status_code == 403 and
              "delegated to" in str(g.json()), f"{g.status_code} {g.json().get('detail')}")
    else:
        print("(NEXA_AGENT_GATEWAY_TOKEN not loaded: skipping the checks made as the runtime, 5b and 5c)")
    # 6, 7. delegated tokens out of place
    dtok = tok("check:sec-student", ["student"], a.school, typ="delegated", act=f"agent:{TUTOR}@0.2.0",
               scope=["curriculum.learner"], nexa_aud="nexa-tool-gateway")
    r = c.get("/v1/agents", headers=h(dtok))
    check("6 delegated token replayed at the API Gateway: 401", r.status_code == 401, f"{r.status_code}")
    other_aud = tok("check:sec-student", ["student"], a.school, typ="delegated", act=f"agent:{TUTOR}@0.2.0",
                    scope=["curriculum.learner"], nexa_aud="nexa-memory")
    k = httpx.post(f"{a.knowledge.rstrip('/')}/v1/knowledge/query", json={"query": "photosynthesis"},
                   headers=h(other_aud), timeout=60)
    check("7 delegated token for another service at Knowledge: 401", k.status_code == 401, f"{k.status_code}")
    # 8, 9. tenancy
    r = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false",
               json={"input": {"topic": "Ratios", "grade": "11"}}, headers=h(teacher))
    rid = r.json().get("run_id")
    run = {}
    for _ in range(200):
        run = c.get(f"/v1/workflow-runs/{rid}?trace=false", headers=h(teacher)).json()
        if run.get("status") != "running":
            break
        time.sleep(2)
    if run.get("status") == "waiting_approval":
        x = c.post(f"/v1/workflow-runs/{rid}/decision", json={"decision": "approve"}, headers=h(teacher_other))
        check("8 reviewer from another school: refused", x.status_code == 403, f"{x.status_code} {detail(x)}")
        seen = c.get(f"/v1/workflow-runs/{rid}?trace=false", headers=h(teacher_other)).status_code
        check("  and cannot see the run", seen == 404, str(seen))
        y = c.post(f"/v1/workflow-runs/{rid}/decision", json={"decision": "approve"}, headers=h(teacher_same))
        check("9 reviewer from the same school: allowed", y.status_code == 200 and y.json().get("status") ==
              "succeeded", f"{y.status_code} {y.json().get('status')}")
    else:
        check("8 reviewer from another school: refused", False, f"run did not reach review: {run.get('status')}")

    # 10. the decision log
    r = c.get("/v1/policy/decisions", headers=h(student))
    check("10 decision log is for auditors only", r.status_code == 403, str(r.status_code))
    rows = c.get("/v1/policy/decisions", params={"decision": "deny", "limit": 200}, headers=h(auditor)).json()
    rows = [x for x in rows.get("decisions", []) if x["ts"] >= t0 - 1]
    rules = {(x["action"], x["rule"]) for x in rows}
    want = {("run", "role"), ("invoke", "scopes"), ("decide", "tenancy")}
    check("   every refusal recorded with its rule", want <= rules, ", ".join(sorted(f"{a_}:{r_}" for a_, r_ in rules)))
    print("\nPolicy decisions (denied) in this check:")
    for x in rows[:20]:
        print(f"  {x['decision_id']}  {x['action']:<8} {x['rule']:<10} agent={x['agent'] or '-':<22} "
              f"user={x['actor']:<26} school={x['tenant'] or '-':<22} {x['reason'][:70]}")

    print()
    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {str(det)[:140]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
