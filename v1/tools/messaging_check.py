"""Live check of the inter-agent message and event contract (NEXA-M3-005).

Runs the bounded multi-agent workflow (nexa-wf-lesson-explainer) on a running
NEXA through the API Gateway, as two teachers, then reads the run's whole
conversation and validates it here, independently of the server, against
nexa.message/1.0:

  - every envelope and payload validates against the versioned schemas
  - one correlation id (the run) and one trace id on every message
  - every request comes from the orchestrator, carries a deadline, attempt,
    idempotency key, the user and the permission
  - every result or error answers a recorded request, from its receiver,
    back to the orchestrator
  - the delegate's request is caused by the orchestrator's decision, which is
    caused by the coordinator's result: no message from agent to agent
  - every causation chain leads back to the run's first event
  - starting again with the same Idempotency-Key returns the same run

    (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     python -m v1.tools.messaging_check)
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

from v1.agent_runtime.messaging import contract as mc
from v1.identity.tokens import issue_token


def hdr(sub, roles):
    return {"Authorization": "Bearer " + issue_token(sub, roles, "user", 3600)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--topic", default="Simple interest")
    ap.add_argument("--grade", default="11")
    a = ap.parse_args(argv)
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=60)
    t1, t2 = hdr("check:msg-teacher-1", ["teacher"]), hdr("check:msg-teacher-2", ["teacher"])
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    schema = c.get("/v1/messaging/schema", headers=t1).json()
    check("server publishes nexa.message/1.0", schema == mc.json_schema(),
          f"{schema.get('title')}; payloads: {', '.join(sorted(schema.get('x-nexa-payloads', {})))}")

    key = "msg-check-" + uuid.uuid4().hex[:12]
    body = {"input": {"topic": a.topic, "grade": a.grade}}
    r = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false", json=body,
               headers=t1 | {"Idempotency-Key": key})
    if r.status_code != 202:
        print(f"could not start: {r.status_code} {r.text[:300]}")
        return 1
    run_id = r.json()["run_id"]
    again = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false", json=body,
                   headers=t1 | {"Idempotency-Key": key})
    check("same Idempotency-Key: same run, not a second one",
          again.status_code == 200 and again.json().get("run_id") == run_id and again.json().get("duplicate"),
          f"{again.status_code} {again.json().get('run_id')}")
    other = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false", json={"input": {**body["input"],
                                                                                              "grade": "12"}},
                   headers=t1 | {"Idempotency-Key": key})
    check("same key, different input: refused 409", other.status_code == 409, str(other.status_code))

    run = {}
    for _ in range(300):
        run = c.get(f"/v1/workflow-runs/{run_id}?trace=false", headers=t1).json()
        if run["status"] != "running":
            break
        time.sleep(2)
    if run["status"] == "waiting_approval":
        c.post(f"/v1/workflow-runs/{run_id}/decision", json={"decision": "approve",
                                                              "comment": "NEXA-M3-005 live check"}, headers=t2)
        for _ in range(60):
            run = c.get(f"/v1/workflow-runs/{run_id}?trace=false", headers=t1).json()
            if run["status"] != "running":
                break
            time.sleep(1)
    check("multi-agent run completes", run["status"] == "succeeded", run["status"] + (f" {run.get('error')}"
                                                                                      if run.get("error") else ""))

    conv = c.get(f"/v1/workflow-runs/{run_id}/messages", headers=t1).json()
    rows = conv["messages"]
    ms = [row["message"] for row in rows]
    by_id = {m["message_id"]: m for m in ms}

    # Independent validation, here, with the contract in this checkout.
    invalid = []
    for row in rows:
        try:
            mc.Message.model_validate(row["message"])
        except Exception as exc:  # noqa: BLE001
            invalid.append(f"#{row['seq']}: {str(exc)[:120]}")
    kinds = {k: sum(1 for m in ms if m["kind"] == k) for k in ("request", "result", "error", "event")}
    check("every envelope and payload valid (checked here)", ms and not invalid,
          f"{len(ms)} messages: " + ", ".join(f"{v} {k}" for k, v in kinds.items()) + (f"; {invalid[:2]}"
                                                                                       if invalid else ""))
    check("server-side check agrees", conv["check"]["valid"], "; ".join(conv["check"]["problems"][:3]))
    check("one correlation id and one trace id",
          {m["correlation_id"] for m in ms} == {run_id} and {m["trace_id"] for m in ms} == {run["trace_id"]},
          f"correlation {run_id}, trace {run['trace_id']}")
    reqs = [m for m in ms if m["kind"] == "request"]
    check("requests only from the orchestrator, fully specified",
          reqs and all(q["sender"]["kind"] == "orchestrator" and q.get("deadline") and q.get("idempotency_key")
                       and q.get("attempt") and q.get("principal", {}).get("subject") == "check:msg-teacher-1"
                       and q.get("permission") for q in reqs),
          ", ".join(f"{q['receiver']['id']}({q['payload']['role']}) attempt {q['attempt']}" for q in reqs))
    replies = [m for m in ms if m["kind"] in ("result", "error")]
    check("every reply answers its request, from its receiver",
          replies and all(m.get("in_reply_to") in by_id and by_id[m["in_reply_to"]]["receiver"]["id"] ==
                          m["sender"]["id"] and m["receiver"]["kind"] == "orchestrator" for m in replies),
          f"{len(replies)} replies")
    check("no agent-to-agent message",
          not any(m["sender"]["kind"] == "agent" and (m.get("receiver") or {}).get("kind") == "agent" for m in ms),
          "agents only answer the orchestrator")
    chain_ok, delegated = True, []
    for q in reqs:
        if q["payload"]["role"] != "delegate":
            continue
        decision = by_id.get(q.get("causation_id") or "")
        coord = by_id.get((decision or {}).get("causation_id") or "")
        ok = bool(decision and decision["type"] == "delegation.choice" and decision["sender"]["kind"] == "orchestrator"
                  and coord and coord["kind"] == "result" and coord["sender"]["kind"] == "agent")
        chain_ok &= ok
        if decision:
            delegated.append(f"{coord['sender']['id'] if coord else '?'} proposed -> orchestrator decided "
                             f"{decision['payload'].get('action')} -> request to {q['receiver']['id']}")
    check("delegation goes through the orchestrator", chain_ok and delegated, "; ".join(delegated))
    first = ms[0]["message_id"] if ms else None
    rooted = True
    for m in ms:
        cur, hops = m, 0
        while cur.get("causation_id") and hops < 1000:
            cur, hops = by_id.get(cur["causation_id"], {}), hops + 1
        rooted &= cur.get("message_id") == first
    check("every causation chain leads to run.started", rooted and ms and ms[0]["type"] == "run.started",
          f"root {first}")

    print(f"\nConversation of {run_id} ({len(ms)} messages; requests, results, errors and the delegation decisions):")
    for row in rows:
        m = row["message"]
        if m["kind"] == "event" and m["type"] != "delegation.choice":
            continue
        to = f"{m['receiver']['kind']}:{m['receiver']['id']}" if m.get("receiver") else "(event log)"
        extra = ""
        if m["kind"] == "request":
            extra = f"attempt {m['attempt']}/{m['max_attempts']} key …{m['idempotency_key'][-24:]} " \
                    f"deadline +{m['deadline'] - m['created_at']:.0f}s"
        elif m["kind"] == "result":
            extra = f"reply to {m['in_reply_to'][-8:]} {m['payload'].get('latency_ms')} ms"
        elif m["kind"] == "error":
            extra = f"{m['error']['code']} retryable={m['error']['retryable']}"
        else:
            p = m["payload"]
            extra = f"decision {p['decision']}: {p.get('action')} accepted={p['accepted']}"
        print(f"  {row['seq']:>3} {m['kind']:<7} {m['type']:<18} {m['sender']['kind']}:{m['sender']['id']:<18} -> "
              f"{to:<26} {extra}  [{row['status'] or '-'}]")

    print()
    width = max(len(n) for n, _, _ in results)
    for n, ok, det in results:
        print(f"{'PASS' if ok else 'FAIL'}  {n:<{width}}  {det[:150]}")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
