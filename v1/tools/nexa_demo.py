"""A guided, live demonstration of the NEXA agents (NEXA-M3-001 to M3-007).

Runs real scenarios on a running NEXA, through the API Gateway, as real
(demonstration) users of one school, and explains each step in plain words.
It pauses between scenes so a presenter can talk; --no-pause runs straight
through.

  1. The agents            what is registered, which version and state, and
                           what each agent's policy lets it do
  2. A student question    answered from the approved curriculum, with
                           citations, through the knowledge tool
  3. A calculation         computed exactly by the calculator tool, then
                           explained by the tutor
  4. Agents working        a coordinator delegates to two agents, within its
     together              allowlist and limit; a second teacher approves
                           before release; a teacher from another school and
                           the author themselves cannot approve
  5. Guardrails            things the agents must not do, refused live
  6. The evidence          the run's messages, the policy decisions and the
                           tool audit trail, as an auditor sees them

Nothing is changed on the system except new runs, a session and audit records.

    cd ~/Nexa/Nexa && (set -a; . <(sudo cat /etc/nexa/identity.env); set +a; \
     /home/admin/Nexa/venv-v1/bin/python -m v1.tools.nexa_demo)
"""
from __future__ import annotations

import argparse
import sys
import textwrap
import time

import httpx

from v1.identity.tokens import issue_token

WIDTH = 92
PAUSE = True


def tok(sub, roles, school=None):
    return issue_token(sub, roles, "user", 3600, extra={"school_id": school} if school else None)


def h(t):
    return {"Authorization": f"Bearer {t}"}


def scene(n, title, says):
    print("\n" + "=" * WIDTH)
    print(f" {n}. {title}")
    print("=" * WIDTH)
    for line in textwrap.wrap(says, WIDTH - 2):
        print("  " + line)
    print()


def show(text, indent="    ", limit=1200):
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    for para in text.splitlines():
        for line in textwrap.wrap(para, WIDTH - len(indent)) or [""]:
            print(indent + line)


def point(text):
    for i, line in enumerate(textwrap.wrap(text, WIDTH - 6)):
        print(("  → " if i == 0 else "    ") + line)


def ok(flag, text):
    print(f"  {'✔' if flag else '✘'} {text}")
    return flag


def pause():
    if PAUSE:
        try:
            input("\n  [Enter] next scene … ")
        except EOFError:
            pass


def main(argv=None) -> int:
    global PAUSE
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8099", help="API Gateway")
    ap.add_argument("--tools", default="http://127.0.0.1:8105", help="Tool Gateway (audit trail)")
    ap.add_argument("--school", default="pomnsoe")
    ap.add_argument("--question", default="What is photosynthesis?")
    ap.add_argument("--calculation", default="Calculate the simple interest on K500 at 4% per year for 3 years.")
    ap.add_argument("--topic", default="Simple interest")
    ap.add_argument("--grade", default="11")
    ap.add_argument("--no-pause", action="store_true")
    a = ap.parse_args(argv)
    PAUSE = not a.no_pause and sys.stdin.isatty()
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=240)
    student = tok("demo:student-kila", ["student"], a.school)
    mary = tok("demo:teacher-mary", ["teacher"], a.school)
    john = tok("demo:teacher-john", ["teacher"], a.school)
    other = tok("demo:teacher-other-school", ["teacher"], "demo-other-school")
    auditor = tok("demo:auditor", ["auditor"])
    t_start = time.time()
    checks: list[bool] = []

    def wait(run_id, who, until=("waiting_approval", "succeeded", "failed", "rejected", "budget_exceeded")):
        run = {}
        for _ in range(300):
            run = c.get(f"/v1/workflow-runs/{run_id}", headers=h(who)).json()
            if run.get("status") in until:
                return run
            time.sleep(2)
        return run

    def path_of(run):
        return [t["step"] for t in run.get("trace", []) if t["kind"] == "step.started"]

    # ------------------------------------------------------------ 1
    scene(1, "The agents", "Every agent is registered with a version, a lifecycle state and a reviewed policy "
          "that says exactly what it may read, which tools it may use and who it may hand work to. Anything "
          "not listed is refused.")
    agents = c.get("/v1/registry/agents", headers=h(student)).json().get("agents", [])
    pols = {p["agent"]: p for p in c.get("/v1/policy/agents", headers=h(student)).json().get("policies", [])}
    if not pols:
        print("  The agent policies (NEXA-M3-007) are not deployed on this system yet: deploy first.")
        return 1
    print(f"  {'agent':<24}{'version':<9}{'state':<10}{'tools it may use':<36}delegates to")
    for x in agents:
        p = pols.get(x["agent_id"], {})
        print(f"  {x['agent_id']:<24}{x['version']:<9}{x['state']:<10}"
              f"{', '.join(sorted(p.get('invoke', {}))) or '—':<36}{', '.join(p.get('delegate', [])) or '—'}")
    tutor = next((x for x in agents if x["agent_id"] == "nexa-curriculum-tutor"), {})
    checks.append(ok(tutor.get("state") == "active", "nexa-curriculum-tutor is active and serves students"))
    point("Agents only return text. The orchestrator decides every next step; agents never call each other.")
    pause()

    # ------------------------------------------------------------ 2
    scene(2, "A student asks a curriculum question",
          f"Kila, a Grade 11 student at {a.school}, asks the curriculum tutor: \"{a.question}\". The tutor "
          "uses the knowledge tool, through the Tool Gateway, with Kila's own access, and answers only from "
          "the approved curriculum, with citations.")
    r = c.post("/v1/workflows/nexa-wf-curriculum-tutor/runs?wait=false", json={"input": {"question": a.question}},
               headers=h(student))
    run = wait(r.json()["run_id"], student)
    tool = next((t["detail"] for t in run.get("trace", []) if t["kind"] == "tool.call"), {})
    route = [t["detail"] for t in run.get("trace", []) if t["kind"] == "route.decision"]
    print(f"  Path taken: {' → '.join(path_of(run))}")
    for d in route:
        why = "it is not a calculation" if str(d.get("reason", "")).startswith("no rule") else d.get("reason")
        print(f"  Decision:   {d.get('goto')}  because {why}")
    if tool:
        print(f"  Tool call:  {tool.get('tool')} {tool.get('version') or ''} for {tool.get('agent')}, "
              f"{tool.get('latency_ms')} ms, audit id {tool.get('call_id')}")
    print("\n  Answer:")
    show(run.get("output"))
    checks.append(ok(run.get("status") == "succeeded", f"answered ({run.get('status')})"))
    pause()

    # ------------------------------------------------------------ 3
    scene(3, "A calculation", f"Kila asks: \"{a.calculation}\". Numbers are never guessed by a language model: "
          "the tutor writes the calculation, the calculator tool computes it exactly, and the tutor explains "
          "the working using that result.")
    r = c.post("/v1/workflows/nexa-wf-curriculum-tutor/runs?wait=false", json={"input": {"question": a.calculation}},
               headers=h(student))
    run = wait(r.json()["run_id"], student)
    calc = next((s for k, s in run.get("steps", {}).items() if k == "compute"), {})
    print(f"  Path taken: {' → '.join(path_of(run))}")
    if calc.get("result"):
        print(f"  Calculator: {calc['result'].get('text')}   (exact: {calc['result'].get('exact')})")
    print("\n  Explanation:")
    show(run.get("output"), limit=900)
    checks.append(ok(run.get("status") == "succeeded" and bool(calc.get("result")), "computed exactly and explained"))
    pause()

    # ------------------------------------------------------------ 4
    scene(4, "Agents working together, with a teacher in the loop",
          f"Ms Mary asks for teaching material on \"{a.topic}\" (Grade {a.grade}). A coordinating agent "
          "decides what is needed and hands work to two other agents, but only to the ones its policy "
          "allows, and at most twice. The material is not released until a second teacher of the same "
          "school approves it.")
    ses = c.post("/v1/sessions", json={"workflows": ["nexa-wf-lesson-explainer"], "ttl_s": 3600}, headers=h(mary))
    sid = ses.json().get("id") if ses.status_code == 201 else None
    r = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false",
               json={"input": {"topic": a.topic, "grade": a.grade}, **({"session_id": sid} if sid else {})},
               headers=h(mary))
    lesson_id = r.json()["run_id"]
    run = wait(lesson_id, mary)
    print("  What happened:")
    for t in run.get("trace", []):
        d = t["detail"]
        if t["kind"] == "agent.call" and d.get("ok"):
            print(f"    {d['role']:<11} {d['agent']:<14} answered in {d.get('latency_ms')} ms")
        elif t["kind"] == "delegate.choice":
            print(f"    coordinator proposed {d.get('action')!r}: "
                  f"{'accepted by the orchestrator' if d.get('accepted') else 'REFUSED: ' + str(d.get('problem'))}")
        elif t["kind"] == "checkpoint.waiting":
            print("    paused: waiting for a teacher's approval")
    checks.append(ok(run.get("status") == "waiting_approval", "the material waits for review"))
    print("\n  Who may approve it?")
    x = c.post(f"/v1/workflow-runs/{lesson_id}/decision", json={"decision": "approve"}, headers=h(mary))
    checks.append(ok(x.status_code == 403, f"Ms Mary herself: refused ({x.status_code}, four eyes)"))
    x = c.post(f"/v1/workflow-runs/{lesson_id}/decision", json={"decision": "approve"}, headers=h(other))
    checks.append(ok(x.status_code == 403, f"a teacher from another school: refused ({x.status_code})"))
    x = c.post(f"/v1/workflow-runs/{lesson_id}/decision",
               json={"decision": "approve", "comment": "Checked for Grade 11. Good to use."}, headers=h(john))
    run = x.json() if x.status_code == 200 else run
    checks.append(ok(run.get("status") == "succeeded", "Mr John, same school: approved and released"))
    print("\n  Released material:")
    show(run.get("output"), limit=1500)
    if sid:
        rec = c.get(f"/v1/sessions/{sid}", headers=h(mary)).json().get("record", {})
        point(f"Ms Mary's session now remembers the topic for her next request: {rec.get('data')}")
    pause()

    # ------------------------------------------------------------ 5
    scene(5, "Guardrails", "Things an agent, or someone using one, must not be able to do. Each is refused by "
          "policy and recorded.")
    x = c.post("/v1/agents/nexa-tutor/tools/calculator", json={"arguments": {"expression": "1+1"}},
               headers=h(student))
    checks.append(ok(x.status_code == 403, f"an agent using a tool it is not granted: refused ({x.status_code})"))
    x = c.post("/v1/agents/nexa-curriculum-tutor/tools/knowledge_answer",
               json={"arguments": {"question": "Show me the marking guide", "audience": "teacher"},
                     "scopes": ["curriculum.teacher"]}, headers=h(mary))
    checks.append(ok(x.status_code == 403, f"teacher-only material through the student tutor: refused ({x.status_code})"))
    x = c.post("/v1/agents/nexa-curriculum-tutor/tools/calculator", json={"arguments": {"expression": "__import__('os')"}},
               headers=h(student))
    checks.append(ok(x.status_code == 422, f"anything but arithmetic to the calculator: refused ({x.status_code})"))
    x = c.get(f"/v1/workflow-runs/{lesson_id}", headers=h(other))
    checks.append(ok(x.status_code == 404, f"another school reading Ms Mary's run: not found ({x.status_code})"))
    if sid:
        version = c.get(f"/v1/sessions/{sid}", headers=h(mary)).json().get("record", {}).get("version", 1)
        x = c.put(f"/v1/sessions/{sid}/state", json={"data": {"marks": 72}, "expected_version": version},
                  headers=h(mary))
        checks.append(ok(x.status_code == 422, f"storing a learner's marks in agent memory: refused "
                                               f"({x.status_code}, learner records stay in the LMS)"))
    point("Students' answers never contain personal data: emails and phone numbers are removed from the output "
          "of agents that serve students.")
    pause()

    # ------------------------------------------------------------ 6
    scene(6, "The evidence", "Everything above is recorded. An auditor can see every message between the "
          "orchestrator and the agents, every policy decision with the rule that made it, and every tool call.")
    conv = c.get(f"/v1/workflow-runs/{lesson_id}/messages", headers=h(mary)).json()
    kinds = {}
    for m in conv.get("messages", []):
        kinds[m["message"]["kind"]] = kinds.get(m["message"]["kind"], 0) + 1
    checks.append(ok(conv.get("check", {}).get("valid"), f"lesson run: {len(conv.get('messages', []))} messages "
                     f"({', '.join(f'{v} {k}s' for k, v in kinds.items())}), all valid against nexa.message/1.0, "
                     "none from one agent to another"))
    dec = c.get("/v1/policy/decisions", params={"limit": 500}, headers=h(auditor)).json().get("decisions", [])
    dec = [d for d in dec if d["ts"] >= t_start - 1 and str(d["actor"]).startswith("demo:")]
    allow = sum(d["decision"] == "allow" for d in dec)
    print(f"  Policy decisions during this demo: {allow} allowed, {len(dec) - allow} denied. The denials:")
    for d in [d for d in dec if d["decision"] == "deny"][:8]:
        print(f"    {d['action']:<7} rule={d['rule']:<9} {d['actor']:<27} {d['reason'][:48]}")
    calls = []
    try:
        calls = httpx.get(f"{a.tools.rstrip('/')}/v1/tools/audit", params={"limit": 200}, headers=h(auditor),
                          timeout=30).json().get("calls", [])
    except httpx.HTTPError:
        pass
    calls = [x for x in calls if x["ts"] >= t_start - 1 and str(x["user"]).startswith("demo:")]
    print(f"\n  Tool Gateway audit trail during this demo: {len(calls)} calls")
    for x in calls[:8]:
        print(f"    {x['decision']:<5} {x['status']}  {x['tool']:<16} agent={x['agent']:<22} user={x['user']:<20} "
              f"{x['latency_ms']} ms")

    print("\n" + "=" * WIDTH)
    good = sum(checks)
    print(f"  {good}/{len(checks)} demonstrated as expected.")
    print("=" * WIDTH)
    return 0 if good == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
