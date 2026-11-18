"""Move an agent version through its lifecycle in the registry (NEXA-M3-002).

An approval is a governance decision: run this only when the named approver
has approved the version. Every step is recorded in the registry's audit
trail with the approver's name and the identity that ran it.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     python -m v1.tools.agent_lifecycle nexa-curriculum-tutor 0.2.0 --to active \
       --approved-by "Full Name" --actor "user:your-name")
"""
from __future__ import annotations

import argparse
import sys

import httpx

from v1.identity.tokens import issue_token

PATH = {"review": ["review"], "approved": ["review", "approved"], "active": ["review", "approved", "active"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("agent")
    ap.add_argument("version")
    ap.add_argument("--to", required=True, choices=sorted(PATH))
    ap.add_argument("--approved-by", help="the approver's name (required to reach approved or active)")
    ap.add_argument("--actor", required=True, help="who is running this, for the audit trail (user:name)")
    ap.add_argument("--reason", default="")
    ap.add_argument("--url", default="http://127.0.0.1:8102", help="Agent Runtime")
    a = ap.parse_args(argv)
    if a.to != "review" and not a.approved_by:
        ap.error("--approved-by is required to approve or activate")
    h = {"Authorization": "Bearer " + issue_token(a.actor, ["admin"], "user", 600)}
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=30)
    cur = c.get(f"/v1/registry/agents/{a.agent}/versions/{a.version}", headers=h)
    if cur.status_code != 200:
        print(f"{a.agent} {a.version}: {cur.status_code} {cur.text[:200]}")
        return 1
    state = cur.json()["manifest"]["lifecycle"]["state"]
    steps = PATH[a.to]
    steps = steps[steps.index(state) + 1:] if state in steps else steps
    for s in steps:
        r = c.put(f"/v1/registry/agents/{a.agent}/versions/{a.version}/state", headers=h,
                  json={"state": s, "approved_by": a.approved_by, "reason": a.reason})
        print(f"{a.agent} {a.version}: -> {s}: {r.status_code} {r.json()}")
        if r.status_code != 200:
            return 1
    print(f"{a.agent} {a.version} is {a.to if steps else state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
