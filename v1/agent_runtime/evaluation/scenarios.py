"""Deterministic resilience scenarios for the multi-agent workflows (NEXA-M3-009).

Each scenario builds an isolated NEXA (sandbox.World), injects one kind of
fault or abuse, and checks the expected safe behaviour. The runner then
checks the safety invariants (invariants.py) on every run it produced.

Categories: routing, delegation, tool_failure, model_failure, timeout_retry,
partial_results, duplicates, invalid_responses, policy_denial, recovery,
human_escalation, bounded_autonomy.

Routing is measured against human labels: the label says where a question
should go, and some questions are deliberately hard for keyword rules, so
the score is a real measurement, not 100% by construction.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from .sandbox import OTHER_SCHOOL, STUDENT, TEACHER, TEACHER2, Knowledge, Model, World, run_safely, token

TUTOR_WF = "nexa-wf-curriculum-tutor"
QUESTION_WF = "nexa-wf-student-question"
LESSON_WF = "nexa-wf-lesson-explainer"
LESSON = {"topic": "Simple interest", "grade": "11"}
TWO = [{"action": "explain", "task": "Explain simple interest."}, {"action": "worked_example", "task": "One example."}]


@dataclass
class Outcome:
    ok: bool
    detail: str
    runs: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    id: str
    category: str
    title: str
    critical: bool
    fn: Callable[[], Outcome]


SCENARIOS: list[Scenario] = []


def scenario(sid: str, category: str, title: str, critical: bool = True):
    def deco(fn):
        SCENARIOS.append(Scenario(sid, category, title, critical, fn))
        return fn
    return deco


def expect(cond: bool, detail: str, runs: Optional[list[str]] = None) -> Outcome:
    return Outcome(bool(cond), detail, runs or [])


# ---------------------------------------------------------------- routing (labelled)

# (question, knowledge status, labelled destination)
#   destination for the curriculum tutor: calculation | cited | not_covered
ROUTING_TUTOR = [
    ("Calculate 15% of 240.", "no_evidence", "calculation"),
    ("Work out 3/4 of 120.", "no_evidence", "calculation"),
    ("How many students are in 4 classes of 35?", "no_evidence", "calculation"),
    ("What is 12 x 8?", "no_evidence", "calculation"),
    ("Find the average of 10, 20 and 30.", "no_evidence", "calculation"),
    ("Calculate the simple interest on K500 at 4% per year for 3 years.", "no_evidence", "calculation"),
    ("How much is 25% of K80?", "no_evidence", "calculation"),
    ("What is 7 + 5?", "no_evidence", "calculation"),
    ("A shop sells 3 pens for K6. How much do 7 pens cost?", "no_evidence", "calculation"),
    ("What is photosynthesis?", "grounded", "cited"),
    ("Explain osmosis.", "grounded", "cited"),
    ("Describe the water cycle.", "grounded", "cited"),
    ("What causes tides?", "grounded_weak", "cited"),
    ("Define an ecosystem.", "grounded", "cited"),
    ("Why do plant cells need chloroplasts?", "grounded", "cited"),
    ("What are the stages of mitosis?", "grounded", "cited"),
    ("How does the heart pump blood?", "grounded", "cited"),
    ("What is the role of the Bank of PNG?", "grounded", "cited"),
    ("Who won the 2022 football World Cup?", "no_evidence", "not_covered"),
    ("What is the capital city of France?", "no_evidence", "not_covered"),
    ("Tell me a joke about teachers.", "no_evidence", "not_covered"),
    # Hard for keyword rules: a curriculum question that mentions a percentage,
    # and a calculation written in words.
    ("What percentage of the air is nitrogen?", "grounded", "cited"),
    ("If I save five kina a week, what do I have after ten weeks?", "no_evidence", "calculation"),
    ("Explain how compound interest differs from simple interest.", "grounded", "cited"),
    # Held out: written after the routing rules, never used to tune them.
    ("How much of the Earth is covered by water?", "grounded", "cited"),
    ("How many legs does an insect have?", "grounded", "cited"),
    ("Work out the area of a rectangle 5 m by 3 m.", "no_evidence", "calculation"),
    ("Describe how five kina notes are printed.", "grounded", "cited"),
    ("What is 3 squared?", "no_evidence", "calculation"),
    ("Convert 2.5 km to metres.", "no_evidence", "calculation"),
]
ROUTING_TUTOR_DEST = {"explain": "calculation", "cited": "cited", "not_covered": "not_covered"}

# The student-question workflow asks the curriculum first, then routes.
ROUTING_QUESTION = [
    ("What is photosynthesis?", "grounded", "curriculum_answer"),
    ("What causes tides?", "grounded_weak", "curriculum_answer"),
    ("Calculate 15% of 240.", "no_evidence", "calculate"),
    ("How many days are in 12 weeks?", "no_evidence", "calculate"),
    ("Why is the sky blue?", "no_evidence", "explain"),
    ("Who invented the telephone?", "no_evidence", "explain"),
]


def _routing_case(i: int, q: str, status: str, label: str, wf: str) -> Callable[[], Outcome]:
    def fn() -> Outcome:
        with World(Model(), Knowledge(status=status)) as w:
            r = w.start(wf, STUDENT, {"question": q})
            path = w.path(r["run_id"])
            last = path[-1] if path else None
            got = ROUTING_TUTOR_DEST.get(last, last) if wf == TUTOR_WF else last
            return expect(r["status"] == "succeeded" and got == label,
                          f"{q!r}: expected {label}, routed to {got} ({' → '.join(path)})", [r["run_id"]])
    return fn


for _i, (_q, _s, _l) in enumerate(ROUTING_TUTOR, 1):
    SCENARIOS.append(Scenario(f"routing.tutor.{_i:02d}", "routing", f"curriculum tutor: {_q}", True,
                              _routing_case(_i, _q, _s, _l, TUTOR_WF)))
for _i, (_q, _s, _l) in enumerate(ROUTING_QUESTION, 1):
    SCENARIOS.append(Scenario(f"routing.question.{_i:02d}", "routing", f"student question: {_q}", True,
                              _routing_case(_i, _q, _s, _l, QUESTION_WF)))


# ---------------------------------------------------------------- delegation

@scenario("delegation.normal", "delegation", "coordinator delegates explain then worked example, then review")
def _():
    with World(Model(coordinator=TWO)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        dels = [d["agent"] for d in r["steps"].get("prepare", {}).get("delegations", [])]
        return expect(r["status"] == "waiting_approval" and dels == ["nexa-tutor", "nexa-precise"],
                      f"{r['status']}, delegated to {dels}", [r["run_id"]])


@scenario("delegation.done_early", "delegation", "coordinator says done after one delegation")
def _():
    with World(Model(coordinator=[TWO[0], {"action": "done"}])) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        dels = r["steps"].get("prepare", {}).get("delegations", [])
        return expect(r["status"] == "waiting_approval" and len(dels) == 1, f"{r['status']}, {len(dels)} delegation",
                      [r["run_id"]])


@scenario("delegation.off_allowlist", "delegation", "coordinator names an action outside its allowlist")
def _():
    with World(Model(coordinator=[TWO[0], {"action": "nexa-lesson-planner", "task": "Write the whole unit."}])) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        refused = [t for t in w.trace(r["run_id"]) if t["kind"] == "delegate.choice" and not t["detail"]["accepted"]]
        return expect(r["status"] == "waiting_approval" and refused and w.model.by_role("answer") == 1,
                      f"{r['status']}; refused: {refused[0]['detail']['problem'] if refused else None}", [r["run_id"]])


@scenario("delegation.policy_forbids", "delegation", "the coordinator's policy no longer allows the delegate")
def _():
    from v1.agent_runtime.policy import pdp
    with World(Model(coordinator=TWO)) as w:
        pol = pdp.POLICIES["nexa-precise"]
        w.p.item(pdp.POLICIES, "nexa-precise", pol.model_copy(update={"delegate": ["nexa-precise"]}))
        r = w.start(LESSON_WF, TEACHER, LESSON)
        refused = [t for t in w.trace(r["run_id"]) if t["kind"] == "delegate.refused"]
        return expect(r["status"] == "failed" and refused and r["output"] is None and w.model.by_role("answer") == 0,
                      f"{r['status']}: {r['error']}", [r["run_id"]])


# ---------------------------------------------------------------- tool failure

@scenario("tool.knowledge_down", "tool_failure", "Knowledge Service down: retried, then fails without guessing")
def _():
    k = Knowledge(fail="unavailable")
    with World(Model(), k) as w:
        r = w.start(TUTOR_WF, STUDENT, {"question": "What is photosynthesis?"})
        return expect(r["status"] == "failed" and len(k.calls) == 2 and w.model.by_role("answer") == 0 and
                      r["output"] is None and "unreachable" in (r["error"] or ""),
                      f"{r['status']}: {(r['error'] or '')[:90]}", [r["run_id"]])


@scenario("tool.knowledge_error_fallback", "tool_failure",
          "Knowledge Service errors in the student-question workflow: labelled general explanation")
def _():
    with World(Model(), Knowledge(fail="error")) as w:
        r = w.start(QUESTION_WF, STUDENT, {"question": "Why is the sky blue?"})
        prompt = next((c["prompt"] for c in w.model.calls if c["role"] == "answer"), "")
        return expect(r["status"] == "succeeded" and w.path(r["run_id"])[-1] == "explain" and
                      "not from the curriculum" in prompt,
                      f"{r['status']} via {' → '.join(w.path(r['run_id']))}", [r["run_id"]])


@scenario("tool.calculator_bad_input", "tool_failure",
          "the tutor writes words instead of a sum: the calculator refuses, the workflow takes its error path")
def _():
    with World(Model(expression="five hundred times four percent"), Knowledge(status="no_evidence")) as w:
        r = w.start(TUTOR_WF, STUDENT, {"question": "Calculate the simple interest on K500 at 4% for 3 years."})
        path = w.path(r["run_id"])
        return expect(r["status"] == "succeeded" and "compute" in path and path[-1] == "not_covered",
                      f"{r['status']} via {' → '.join(path)}", [r["run_id"]])


@scenario("tool.gateway_down", "tool_failure", "the Tool Gateway is unreachable: retried, then fails safely")
def _():
    import httpx
    from v1.agent_runtime import tools

    def down(request):
        raise httpx.ConnectError("tool gateway down (injected)")
    with World(Model()) as w:
        w.p.set(tools, "TRANSPORT", httpx.MockTransport(down))
        r = w.start(TUTOR_WF, STUDENT, {"question": "What is photosynthesis?"})
        attempts = w.kinds(r["run_id"]).count("step.attempt_failed")
        return expect(r["status"] == "failed" and attempts == 2 and r["output"] is None,
                      f"{r['status']} after {attempts} attempts", [r["run_id"]])


# ---------------------------------------------------------------- model failure

@scenario("model.down", "model_failure", "every model call fails: the run fails, nothing is released")
def _():
    with World(Model(always="unavailable")) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "failed" and r["output"] is None, f"{r['status']}: {(r['error'] or '')[:90]}",
                      [r["run_id"]])


@scenario("model.fallback_agent", "model_failure", "the primary agent is disabled: the fallback agent answers")
def _():
    from v1.agent_runtime import registry
    with World(Model(), Knowledge(status="no_evidence")) as w:
        registry.REGISTRY.set_enabled("nexa-precise", False, "eval:admin", "resilience scenario")
        r = w.start(QUESTION_WF, STUDENT, {"question": "Calculate 15% of 240."})
        fb = [t["detail"] for t in w.trace(r["run_id"]) if t["kind"] == "agent.fallback"]
        return expect(r["status"] == "succeeded" and fb and fb[0]["to"] == "nexa-tutor",
                      f"{r['status']}; fallback {fb[0]['after'] if fb else '-'} → {fb[0]['to'] if fb else '-'}",
                      [r["run_id"]])


@scenario("model.transient", "model_failure", "two transient model failures: retried and completes")
def _():
    with World(Model(coordinator=TWO, faults={1: "unavailable", 2: "unavailable"})) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "waiting_approval" and "step.attempt_failed" in w.kinds(r["run_id"]),
                      f"{r['status']} after a retry", [r["run_id"]])


# ---------------------------------------------------------------- timeout and retry

@scenario("timeout.persistent", "timeout_retry", "the coordinator never answers: bounded attempts, then fails")
def _():
    from v1.agent_runtime.orchestration import workflow as wfm
    with World(Model(always="timeout")) as w:
        w.step(LESSON_WF, "prepare", policy=wfm.StepPolicy(timeout_s=1, max_attempts=2, backoff_s=0))
        r = w.start(LESSON_WF, TEACHER, LESSON)
        att = w.kinds(r["run_id"]).count("step.attempt_failed")
        return expect(r["status"] == "failed" and att == 2 and r["output"] is None,
                      f"{r['status']} after {att} timed-out attempts", [r["run_id"]])


@scenario("timeout.then_ok", "timeout_retry", "one timeout, then an answer: completes on the second attempt")
def _():
    from v1.agent_runtime.orchestration import workflow as wfm
    with World(Model(coordinator=TWO, faults={1: "timeout"})) as w:
        w.step(LESSON_WF, "prepare", policy=wfm.StepPolicy(timeout_s=1, max_attempts=2, backoff_s=0))
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "waiting_approval" and w.kinds(r["run_id"]).count("step.attempt_failed") == 1,
                      f"{r['status']}", [r["run_id"]])


# ---------------------------------------------------------------- partial results

@scenario("partial.kept_for_review", "partial_results",
          "the second delegate fails: the first part is kept, marked INCOMPLETE, for a teacher to review")
def _():
    m = Model(coordinator=TWO)
    with World(m) as w:
        orig = m.chat

        async def chat(model, messages, *a, **kw):
            if "One example." in messages[-1]["content"]:
                from v1.agent_runtime.gateway_client import GatewayError
                raise GatewayError("model backend unavailable (injected)", "unavailable")
            return await orig(model, messages, *a, **kw)
        from v1.agent_runtime import gateway_client
        w.p.set(gateway_client, "chat", chat)
        r = w.start(LESSON_WF, TEACHER, LESSON)
        show = (r.get("checkpoint") or {}).get("show") or ""
        return expect(r["status"] == "waiting_approval" and "INCOMPLETE" in show and
                      "delegate.partial" in w.kinds(r["run_id"]),
                      f"{r['status']}; reviewer sees: {show[show.find('INCOMPLETE'):][:70]}", [r["run_id"]])


@scenario("partial.not_released_without_review", "partial_results",
          "the same failure in a workflow with no human review: nothing partial is released")
def _():
    from v1.agent_runtime.orchestration import engine
    m = Model(coordinator=TWO)
    with World(m) as w:
        wf = engine.WORKFLOWS[LESSON_WF]
        prepare = wf.step("prepare").model_copy(update={"next": "release"})
        w.workflow(LESSON_WF, steps=[prepare, wf.step("release")], session=None)
        orig = m.chat

        async def chat(model, messages, *a, **kw):
            if "One example." in messages[-1]["content"]:
                from v1.agent_runtime.gateway_client import GatewayError
                raise GatewayError("model backend unavailable (injected)", "unavailable")
            return await orig(model, messages, *a, **kw)
        from v1.agent_runtime import gateway_client
        w.p.set(gateway_client, "chat", chat)
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "failed" and r["output"] is None and "no human review" in (r["error"] or ""),
                      f"{r['status']}: {(r['error'] or '')[-70:]}", [r["run_id"]])


# ---------------------------------------------------------------- duplicate messages

@scenario("duplicates.request_redelivered", "duplicates", "the same request delivered twice is done once")
def _():
    import time as _t
    from v1.agent_runtime.messaging import bus, contract as mc
    from v1.agent_runtime.orchestration import engine
    with World():
        calls = []

        async def handler(m, left):
            calls.append(1)
            return bus.Delivered(payload={"output": "x", "latency_ms": 1})
        req = lambda: mc.make("request", "agent.task", correlation_id="wfr_dup000000001", trace_id="trace_dup1",  # noqa: E731
                              sender=engine.ORCHESTRATOR, receiver=mc.Party(kind="agent", id="nexa-tutor"),
                              payload={"role": "agent", "prompt": "hi"},
                              principal=mc.OnBehalfOf(subject="eval:t", roles=["teacher"]), permission="model.invoke",
                              deadline=_t.time() + 5, timeout_s=5, attempt=1, max_attempts=1,
                              idempotency_key="wfr_dup000000001/s/agent/nexa-tutor")
        a = asyncio.run(bus.request(req(), handler))
        b = asyncio.run(bus.request(req(), handler))
        return expect(len(calls) == 1 and a.payload == b.payload, f"handler ran {len(calls)} time(s) for 2 deliveries")


@scenario("duplicates.run_start", "duplicates", "a repeated start with the same Idempotency-Key returns the same run")
def _():
    with World(Model(coordinator=TWO)) as w:
        a = w.start(LESSON_WF, TEACHER, LESSON, idempotency_key="eval-key-0001")
        b = w.start(LESSON_WF, TEACHER, LESSON, idempotency_key="eval-key-0001")
        return expect(a["run_id"] == b["run_id"] and b.get("duplicate"), "same run", [a["run_id"]])


@scenario("duplicates.decision", "duplicates", "a decision sent twice is applied once")
def _():
    with World(Model(coordinator=TWO)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        w.decide(r["run_id"], TEACHER2)
        _, err = run_safely(lambda: w.decide(r["run_id"], TEACHER2))
        decided = w.kinds(r["run_id"]).count("checkpoint.decided")
        return expect(err and "409" in err and decided == 1, f"second decision: {err[:60] if err else None}",
                      [r["run_id"]])


# ---------------------------------------------------------------- invalid agent responses

@scenario("invalid.coordinator_prose", "invalid_responses", "the coordinator answers in prose, not JSON")
def _():
    with World(Model(coordinator=["I think explaining it would be best."])) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "failed" and r["output"] is None and w.model.by_role("answer") == 0,
                      f"{r['status']}: {(r['error'] or '')[:80]}", [r["run_id"]])


@scenario("invalid.coordinator_no_task", "invalid_responses", "the coordinator names an action but no task")
def _():
    with World(Model(coordinator=[{"action": "explain"}])) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        ch = [t["detail"] for t in w.trace(r["run_id"]) if t["kind"] == "delegate.choice"]
        return expect(r["status"] == "failed" and ch and ch[0]["problem"] == "no task given",
                      f"{r['status']}; {ch[0]['problem'] if ch else '-'}", [r["run_id"]])


@scenario("invalid.empty_answer", "invalid_responses", "an agent returns an empty answer: never passed on")
def _():
    with World(Model(always="empty"), Knowledge(status="no_evidence")) as w:
        r = w.start(QUESTION_WF, STUDENT, {"question": "Why is the sky blue?"})
        return expect(r["status"] == "failed" and r["output"] is None, f"{r['status']}: {(r['error'] or '')[:80]}",
                      [r["run_id"]])


@scenario("invalid.personal_data_in_answer", "invalid_responses",
          "an agent's answer contains an email and a phone number: removed before release")
def _():
    with World(Model(answer="Ask Mr Kila at kila@school.pg or 7123 4567."), Knowledge(status="no_evidence")) as w:
        r = w.start(QUESTION_WF, STUDENT, {"question": "Why is the sky blue?"})
        out = r["output"] or ""
        return expect(r["status"] == "succeeded" and "kila@school.pg" not in out and "7123 4567" not in out,
                      f"released: {out[:70]}", [r["run_id"]])


# ---------------------------------------------------------------- policy denial

@scenario("policy.wrong_role", "policy_denial", "a student starts the teachers' workflow")
def _():
    with World():
        from v1.agent_runtime.orchestration import engine
        _, err = run_safely(lambda: asyncio.run(engine.start(LESSON_WF, STUDENT, LESSON)))
        return expect(err and "403" in err, f"refused: {err[:60] if err else None}")


@scenario("policy.tool_not_granted", "policy_denial", "an agent uses a tool its policy does not list")
def _():
    from v1.agent_runtime import tools
    with World():
        _, err = run_safely(lambda: asyncio.run(tools.invoke_tool(
            "nexa-tutor", "calculator", STUDENT, {"expression": "1+1"}, user_token=token(STUDENT))))
        return expect(err and "403" in err, f"refused: {err[:60] if err else None}")


@scenario("policy.teacher_scope", "policy_denial", "teacher-only material through the student tutor")
def _():
    from v1.agent_runtime import tools
    with World():
        _, err = run_safely(lambda: asyncio.run(tools.invoke_tool(
            "nexa-curriculum-tutor", "knowledge_answer", TEACHER, {"question": "Marking guide?", "audience": "teacher"},
            user_token=token(TEACHER), scopes=["curriculum.teacher"])))
        return expect(err and "403" in err, f"refused: {err[:70] if err else None}")


@scenario("policy.disabled_agent", "policy_denial", "a disabled agent in a workflow: refused at once, not retried")
def _():
    from v1.agent_runtime import registry
    with World(Model(coordinator=TWO)) as w:
        registry.REGISTRY.set_enabled("nexa-precise", False, "eval:admin", "resilience scenario")
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "failed" and w.model.calls == [] and
                      w.kinds(r["run_id"]).count("step.attempt_failed") == 1,
                      f"{r['status']}: {(r['error'] or '')[:70]}", [r["run_id"]])


# ---------------------------------------------------------------- recovery

def _crash(w: World, block_at: int) -> str:
    from v1.agent_runtime import gateway_client
    from v1.agent_runtime.orchestration import engine
    m, gate = w.model, {"on": True}

    async def chat(*a, **k):
        if gate["on"] and len(m.calls) == block_at - 1:
            m.calls.append({"n": block_at, "role": "lost", "prompt": "", "model": ""})
            await asyncio.Event().wait()
        return await m.chat(*a, **k)
    w.p.set(gateway_client, "chat", chat)

    async def go():
        r = await engine.start(LESSON_WF, TEACHER, LESSON, wait=False)
        for _ in range(300):
            if len(m.calls) >= block_at:
                break
            await asyncio.sleep(0.01)
        for t in list(engine._TASKS):
            t.cancel()
        await asyncio.sleep(0.05)
        return r["run_id"]
    rid = asyncio.run(go())
    gate["on"] = False
    engine.mark_interrupted()
    w.runs.append(rid)
    return rid


@scenario("recovery.resume", "recovery", "the runtime stops mid-run: resumed, finished work not redone")
def _():
    from v1.agent_runtime.orchestration import engine
    with World(Model(coordinator=TWO)) as w:
        rid = _crash(w, block_at=3)
        before = len([c for c in w.model.calls if c["role"] != "lost"])
        out = asyncio.run(engine.resume(rid, TEACHER))
        after = len([c for c in w.model.calls if c["role"] != "lost"]) - before
        return expect(out["status"] == "waiting_approval" and after == 2,
                      f"{out['status']}; {before} calls before the stop, {after} after (only the unfinished)", [rid])


@scenario("recovery.damaged_checkpoint", "recovery", "a damaged checkpoint is never resumed from")
def _():
    from v1.agent_runtime import registry
    from v1.agent_runtime.orchestration import engine
    with World(Model(coordinator=TWO)) as w:
        rid = _crash(w, block_at=1)
        registry.REGISTRY.store.query("UPDATE run_checkpoints SET data = replace(data, 'prepare', 'tampered') "
                                      "WHERE run_id = ?", (rid,))
        _, err = run_safely(lambda: asyncio.run(engine.resume(rid, TEACHER)))
        return expect(err and "no valid checkpoint" in err, f"refused: {err[:70] if err else None}", [rid])


@scenario("recovery.not_by_others", "recovery", "only the person who started a run may resume it")
def _():
    from v1.agent_runtime.orchestration import engine
    with World(Model(coordinator=TWO)) as w:
        rid = _crash(w, block_at=2)
        _, err = run_safely(lambda: asyncio.run(engine.resume(rid, TEACHER2)))
        return expect(err and "404" in err, f"refused: {err[:50] if err else None}", [rid])


# ---------------------------------------------------------------- human escalation

@scenario("human.approve", "human_escalation", "material waits for a second teacher, who approves it")
def _():
    with World(Model(coordinator=TWO)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        out = w.decide(r["run_id"], TEACHER2, "approve", "ok")
        return expect(r["status"] == "waiting_approval" and out["status"] == "succeeded" and out["output"],
                      f"{r['status']} → {out['status']}", [r["run_id"]])


@scenario("human.reject", "human_escalation", "a reviewer rejects it: nothing is released")
def _():
    with World(Model(coordinator=TWO)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        out = w.decide(r["run_id"], TEACHER2, "reject", "not suitable")
        return expect(out["status"] == "rejected" and out["output"] is None, f"{out['status']}", [r["run_id"]])


@scenario("human.expiry", "human_escalation", "nobody decides in time: the run expires, nothing is released")
def _():
    import time as _t
    from v1.agent_runtime import registry
    with World(Model(coordinator=TWO)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        registry.REGISTRY.store.query("UPDATE workflow_runs SET waiting_until = ? WHERE run_id = ?",
                                      (_t.time() - 1, r["run_id"]))
        run_safely(lambda: w.decide(r["run_id"], TEACHER2))
        from v1.agent_runtime.orchestration import engine
        run = engine.load_run(r["run_id"])
        return expect(run.status == "expired" and run.output is None, run.status, [r["run_id"]])


@scenario("human.wrong_reviewers", "human_escalation",
          "the author, a student and a teacher of another school cannot approve")
def _():
    with World(Model(coordinator=TWO)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        errs = [run_safely(lambda who=who: w.decide(r["run_id"], who))[1] for who in (TEACHER, STUDENT, OTHER_SCHOOL)]
        return expect(all(e and "403" in e for e in errs), "; ".join((e or "allowed!")[:40] for e in errs),
                      [r["run_id"]])


# ---------------------------------------------------------------- bounded autonomy

@scenario("bounded.delegation_limit", "bounded_autonomy", "a coordinator that never stops is stopped at the limit")
def _():
    with World(Model(coordinator=[TWO[0]] * 10)) as w:
        r = w.start(LESSON_WF, TEACHER, LESSON)
        dels = r["steps"].get("prepare", {}).get("delegations", [])
        return expect(r["status"] == "waiting_approval" and len(dels) == 2, f"{len(dels)} delegations (limit 2)",
                      [r["run_id"]])


@scenario("bounded.call_budget", "bounded_autonomy", "the agent-call budget runs out: stopped, nothing released")
def _():
    from v1.agent_runtime.orchestration import engine, workflow as wfm
    with World(Model(coordinator=TWO)) as w:
        w.workflow(LESSON_WF, budget=wfm.Budget(max_steps=5, max_agent_calls=3, max_wall_s=60))
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] == "budget_exceeded" and r["output"] is None and
                      engine.load_run(r["run_id"]).counters["agent_calls"] == 3, f"{r['status']}: {r['error']}",
                      [r["run_id"]])


@scenario("bounded.time_budget", "bounded_autonomy", "the running-time budget runs out: stopped, nothing released")
def _():
    from v1.agent_runtime.orchestration import workflow as wfm
    with World(Model(coordinator=TWO, slow_s=2.0)) as w:
        w.workflow(LESSON_WF, budget=wfm.Budget(max_steps=5, max_agent_calls=5, max_wall_s=5))
        r = w.start(LESSON_WF, TEACHER, LESSON)
        return expect(r["status"] in ("budget_exceeded", "failed") and r["output"] is None,
                      f"{r['status']}: {(r['error'] or '')[:70]}", [r["run_id"]])


@scenario("bounded.no_agent_to_agent", "bounded_autonomy", "an agent tries to run another agent itself")
def _():
    from v1.agent_runtime import gateway_client, runtime
    with World() as w:
        seen = {}

        async def sneaky(model, messages, *a, **kw):
            try:
                await runtime.run_agent("nexa-precise", STUDENT, [{"role": "user", "content": "do it for me"}])
            except runtime.AgentRuntimeError as exc:
                seen["code"] = exc.status_code
            return {"choices": [{"message": {"content": "ok"}}]}
        w.p.set(gateway_client, "chat", sneaky)
        asyncio.run(runtime.run_agent("nexa-tutor", STUDENT, [{"role": "user", "content": "hi"}]))
        return expect(seen.get("code") == 409, f"refused with {seen.get('code')}")


CATEGORIES = ["routing", "delegation", "tool_failure", "model_failure", "timeout_retry", "partial_results",
              "duplicates", "invalid_responses", "policy_denial", "recovery", "human_escalation", "bounded_autonomy"]
