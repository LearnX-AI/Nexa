"""Reconstruct an agent execution from its logs and traces (NEXA-M3-008).

For operators: give a workflow run id (wfr_...) or a trace id, and it prints
the run as a tree: steps, agent calls (agent, version, model, tokens,
latency, attempts), tool calls (with the Tool Gateway's record and the
curriculum passages retrieved), routes, policy decisions, handoffs,
checkpoints and errors. Users are pseudonymised and content is replaced by
its size and digest; --forensic (auditors, with --reason) shows content, and
that use is logged.

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \\
     python -m v1.tools.trace_view wfr_0123456789abcdef0123 --operator user:yasaru)
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from v1.agent_runtime.tracing.reconstruct import render_text
from v1.identity.tokens import issue_token


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("id", help="a workflow run id (wfr_...) or a trace id")
    ap.add_argument("--operator", required=True, help="who is looking, for the access log (user:name)")
    ap.add_argument("--forensic", action="store_true", help="content and real subjects (auditor)")
    ap.add_argument("--reason", default="", help="why the forensic view is needed (required with --forensic)")
    ap.add_argument("--json", action="store_true", help="print the nexa.trace/1.0 record")
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    a = ap.parse_args(argv)
    roles = ["auditor"] if a.forensic else ["ai_engineer"]
    h = {"Authorization": "Bearer " + issue_token(a.operator, roles, "user", 600)}
    params = {"view": "forensic", "reason": a.reason} if a.forensic else {}
    kind = "runs" if a.id.startswith("wfr_") else "traces"
    r = httpx.get(f"{a.url.rstrip('/')}/v1/observability/{kind}/{a.id}", params=params, headers=h, timeout=60)
    if r.status_code != 200:
        print(f"{r.status_code}: {r.text[:300]}")
        return 1
    body = r.json()
    if a.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    recs = body["runs"] if kind == "traces" else [body]
    for rec in recs:
        print(render_text(rec))
        print()
    if kind == "traces" and body.get("policy_decisions_outside_runs"):
        print("policy decisions under this trace outside any run:")
        for d in body["policy_decisions_outside_runs"]:
            print(f"  {d['action']} {d['decision']} rule={d['rule']} {d['agent'] or '-'} {d['resource']} {d['actor']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
