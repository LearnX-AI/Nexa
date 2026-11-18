"""Live check of the agent registry and discovery service (NEXA-M3-002).

Runs the acceptance criteria against a running NEXA (through the API Gateway
by default): agents are registered and queried by capability, versions are
immutable, an agent can be disabled and enabled, and every change is in the
audit trail. Leaves the registry as it found it: the only agent it disables
is nexa-lesson-planner (a draft that cannot run anyway), and it enables it
again; it registers nothing new.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     python -m v1.tools.registry_check --url http://127.0.0.1:8099)
"""
from __future__ import annotations

import argparse
import copy
import json
import sys

import httpx

from v1.identity.tokens import issue_token

PROBE = "nexa-lesson-planner"


def hdr(roles, sub):
    return {"Authorization": "Bearer " + issue_token(sub, roles, "user", 900)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway (or Agent Runtime) URL")
    ap.add_argument("--runtime", default="http://127.0.0.1:8102", help="Agent Runtime URL, for /health")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=30)
    student, teacher = hdr(["student"], "check:registry-student"), hdr(["teacher"], "check:registry-teacher")
    eng, admin = hdr(["ai_engineer"], "check:registry-engineer"), hdr(["admin"], "check:registry-admin")
    auditor = hdr(["auditor"], "check:registry-auditor")
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    health = httpx.get(a.runtime.rstrip("/") + "/health", timeout=10).json()
    reg_info = health.get("registry") or {}
    check("runtime reports the registry", bool(reg_info), json.dumps(reg_info))

    # 1. Registered, queried by capability
    agents = c.get("/v1/registry/agents", headers=teacher).json().get("agents", [])
    ids = sorted(x["agent_id"] for x in agents)
    check("agents registered", {"nexa-tutor", "nexa-precise", "nexa-curriculum-tutor", PROBE} <= set(ids),
          ", ".join(f"{x['agent_id']} {x['version']} {x['state']}" for x in agents))
    by_cap = sorted(x["agent_id"] for x in c.get("/v1/registry/agents", params={"capability": "lesson_planning"},
                                                 headers=teacher).json()["agents"])
    check("query by capability lesson_planning", by_cap == [PROBE], ", ".join(by_cap))
    runnable = sorted(x["agent_id"] for x in c.get("/v1/registry/agents", params={"runnable": "true"},
                                                   headers=teacher).json()["agents"])
    check("query runnable", runnable == ["nexa-precise", "nexa-tutor"], ", ".join(runnable))

    # 2. Discovery
    loc = reg_info.get("location")
    d = c.get("/v1/registry/discover", params={"capability": "chat", "location": loc}, headers=student).json()
    found = {x["agent_id"]: x for x in d.get("agents", [])}
    tutor_dep = (found.get("nexa-tutor") or {}).get("deployments") or [{}]
    check("discover chat as a student", "nexa-tutor" in found and PROBE not in found,
          ", ".join(f"{k} @ {v['deployments'][0]['location']} {v['deployments'][0]['endpoint']}"
                    for k, v in found.items()))
    check("tutor deployment healthy", tutor_dep[0].get("healthy") is True,
          json.dumps({k: tutor_dep[0].get(k) for k in ("deployment_id", "ready", "fresh", "checks")}))

    # 3. Versioning: re-registering is idempotent, a changed manifest under the same version is refused
    detail = c.get("/v1/registry/agents/nexa-tutor", headers=eng).json()
    v = detail["version"]
    raw = c.get(f"/v1/registry/agents/nexa-tutor/versions/{v}", headers=eng).json()["manifest"]
    r = c.post("/v1/registry/agents", json=raw, headers=eng)
    check("re-register the same version: unchanged", r.status_code == 201 and r.json().get("result") == "unchanged",
          f"{r.status_code} {r.json()}")
    changed = copy.deepcopy(raw)
    changed["prompt"]["system"] += " (changed without a new version)"
    r = c.post("/v1/registry/agents", json=changed, headers=eng)
    check("changed manifest, same version: refused 409", r.status_code == 409, f"{r.status_code} {r.json()}")
    bad = copy.deepcopy(raw)
    bad.pop("safety")
    r = c.post("/v1/registry/agents", json=bad, headers=eng)
    check("invalid manifest: refused 422", r.status_code == 422, str(r.status_code))
    r = c.post("/v1/registry/agents", json=raw, headers=student)
    check("student cannot register: 403", r.status_code == 403, str(r.status_code))
    check("versions listed", bool(detail.get("versions")), f"nexa-tutor versions {detail.get('versions')}")

    # 4. Disable and enable (the kill switch)
    r = c.post(f"/v1/registry/agents/{PROBE}/disable", json={"reason": "x"}, headers=teacher)
    check("teacher cannot disable: 403", r.status_code == 403, str(r.status_code))
    r = c.post(f"/v1/registry/agents/{PROBE}/disable", json={"reason": "NEXA-M3-002 live check"}, headers=admin)
    check("admin disables", r.status_code == 200, f"{r.status_code} {r.json()}")
    run = c.post(f"/v1/agents/{PROBE}/run", json={"messages": [{"role": "user", "content": "hi"}]}, headers=teacher)
    check("disabled agent refused 423", run.status_code == 423, f"{run.status_code} {run.json().get('detail')}")
    disabled = httpx.get(a.runtime.rstrip("/") + "/health", timeout=10).json()["registry"]["disabled"]
    check("health lists it disabled", PROBE in disabled, str(disabled))
    r = c.post(f"/v1/registry/agents/{PROBE}/enable", json={"reason": "NEXA-M3-002 live check done"}, headers=admin)
    check("admin enables", r.status_code == 200, f"{r.status_code} {r.json()}")
    run = c.post(f"/v1/agents/{PROBE}/run", json={"messages": [{"role": "user", "content": "hi"}]}, headers=teacher)
    check("enabled draft is back to 409 (lifecycle)", run.status_code == 409, f"{run.status_code}")

    # 5. Audit
    ev = c.get(f"/v1/registry/agents/{PROBE}/history", params={"limit": 5}, headers=auditor).json()["events"]
    top = [(e["action"], e["actor"]) for e in ev[:2]]
    check("audit trail records both", top == [("agent.enabled", "check:registry-admin"),
                                              ("agent.disabled", "check:registry-admin")], str(top))

    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {det[:160]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
