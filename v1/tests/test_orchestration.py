"""NEXA-M3-003: orchestration engine and workflow patterns. One deterministic
workflow and one bounded multi-agent workflow, with full traces and no direct
agent-to-agent calls."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import gateway_client, presence, registry, runtime
from v1.agent_runtime.orchestration import engine, workflow as wfm
from v1.identity import tokens
from v1.identity.principal import Principal

STUDENT = Principal("user:student-1", ("student",), "user")
TEACHER = Principal("user:teacher-1", ("teacher",), "user")
TEACHER2 = Principal("user:teacher-2", ("teacher",), "user")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    registry.reload(":memory:")
    engine.load_workflows()

    async def deps():
        return {"model_gateway": {"ok": True}, "knowledge_service": {"ok": True}, "memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", deps)
    monkeypatch.setenv("NEXA_AGENT_INSTANCE", "test-1")
    asyncio.run(presence.beat())
    yield
    registry.reload(":memory:")
    engine.load_workflows()


class Gateway:
    """A scripted Model Gateway: records every call; coordinator prompts get
    the next scripted choice."""

    def __init__(self, choices=(), slow=0.0):
        self.calls, self.choices, self.slow = [], list(choices), slow

    async def chat(self, model, messages, temperature=None, max_tokens=None, trace_id=None):
        text = messages[-1]["content"]
        self.calls.append({"model": model, "prompt": text, "trace_id": trace_id})
        if self.slow:
            await asyncio.sleep(self.slow)
        if "Reply with only one JSON object" in text:
            c = self.choices.pop(0) if self.choices else {"action": "done"}
            return {"choices": [{"message": {"content": c if isinstance(c, str) else json.dumps(c)}}],
                    "x_nexa": {"model": model}}
        return {"choices": [{"message": {"content": f"[{model}] answer"}}], "x_nexa": {"model": model}}


@pytest.fixture
def gw(monkeypatch):
    g = Gateway()
    monkeypatch.setattr(gateway_client, "chat", g.chat)
    return g


def knowledge(monkeypatch, status="grounded", answer="Photosynthesis makes glucose [1].", fail=None):
    calls = []

    async def fake(url, body, auth, trace_id, timeout):
        calls.append(body)
        if fail:
            raise fail
        return {"status": status, "answer": answer, "confidence": "high" if status == "grounded" else "none",
                "citations": [{"n": 1}] if status == "grounded" else []}
    monkeypatch.setattr(engine, "knowledge_answer", fake)
    return calls


def start(wf, principal, inputs, auth="Bearer user-token"):
    return asyncio.run(engine.start(wf, principal, inputs, auth=auth))


def kinds(run):
    return [(t["step"], t["kind"]) for t in engine.trace_of(run["run_id"])]


def path(run):
    return [t["step"] for t in engine.trace_of(run["run_id"]) if t["kind"] == "step.started"]


# ---------------------------------------------------------------- deterministic workflow

def test_deterministic_grounded_answer_needs_no_agent(monkeypatch, gw):
    knowledge(monkeypatch)
    r = start("nexa-wf-student-question", STUDENT, {"question": "What does photosynthesis make?"})
    assert r["status"] == "succeeded" and r["output"] == "Photosynthesis makes glucose [1]."
    assert path(r) == ["curriculum", "route", "curriculum_answer"]
    assert gw.calls == [] and r["counters"]["agent_calls"] == 0
    route = next(t for t in engine.trace_of(r["run_id"]) if t["kind"] == "route.decision")
    assert route["detail"]["goto"] == "curriculum_answer"
    assert route["detail"]["reason"] == "the curriculum evidence supports an answer"
    assert route["detail"]["tested"] == [{"field": "steps.curriculum.status", "value": "grounded"}]


def test_deterministic_calculation_path_and_full_trace(monkeypatch, gw):
    knowledge(monkeypatch, status="no_evidence", answer="Not in the curriculum.")
    r = start("nexa-wf-student-question", STUDENT, {"question": "Calculate 15% of 240"})
    assert r["status"] == "succeeded" and r["output"] == "[nexa-chat-precise] answer"
    assert path(r) == ["curriculum", "route", "calculate"]
    call = next(t for t in engine.trace_of(r["run_id"]) if t["kind"] == "agent.call")
    d = call["detail"]
    assert d["agent"] == "nexa-precise" and d["version"] == "1.0.0" and d["manifest"].startswith("sha256:")
    assert d["trace_id"] == r["trace_id"] and gw.calls[0]["trace_id"] == r["trace_id"]   # one trace end to end
    assert "Calculate 15% of 240" in gw.calls[0]["prompt"]
    assert [k for _, k in kinds(r)][0] == "run.started" and [k for _, k in kinds(r)][-1] == "run.succeeded"


def test_deterministic_same_input_same_path(monkeypatch, gw):
    knowledge(monkeypatch, status="no_evidence")
    paths = {tuple(path(start("nexa-wf-student-question", STUDENT, {"question": "Why is the sky blue?"})))
             for _ in range(3)}
    assert paths == {("curriculum", "route", "explain")}
    r = start("nexa-wf-student-question", STUDENT, {"question": "Why is the sky blue?"})
    disc = next(t for t in engine.trace_of(r["run_id"]) if t["kind"] == "agent.discovered")
    assert disc["detail"]["capability"] == "explanation" and disc["detail"]["chose"] == "nexa-tutor"


def test_knowledge_outage_retries_then_takes_the_error_path(monkeypatch, gw):
    calls = knowledge(monkeypatch, fail=httpx.ConnectError("down"))
    r = start("nexa-wf-student-question", STUDENT, {"question": "Why is the sky blue?"})
    assert len(calls) == 2                                         # max_attempts 2
    assert r["status"] == "succeeded" and path(r) == ["curriculum", "route", "explain"]
    k = [kk for _, kk in kinds(r)]
    assert k.count("step.attempt_failed") == 2 and "step.error_path" in k


def test_fallback_agent_when_the_first_is_disabled(monkeypatch, gw):
    knowledge(monkeypatch, status="no_evidence")
    registry.REGISTRY.set_enabled("nexa-precise", False, "user:admin", "drill")
    r = start("nexa-wf-student-question", STUDENT, {"question": "Calculate 2 + 2"})
    assert r["status"] == "succeeded" and r["output"] == "[nexa-chat-default] answer"
    fb = next(t for t in engine.trace_of(r["run_id"]) if t["kind"] == "agent.fallback")
    assert fb["detail"]["to"] == "nexa-tutor" and "disabled" in fb["detail"]["reason"]
    refused = [t for t in engine.trace_of(r["run_id"]) if t["kind"] == "agent.call" and not t["detail"]["ok"]]
    assert refused[0]["detail"]["status"] == 423


def test_step_timeout_retries_then_fails_the_run(monkeypatch):
    knowledge(monkeypatch, status="no_evidence")
    monkeypatch.setattr(gateway_client, "chat", Gateway(slow=5).chat)
    wf = engine.WORKFLOWS["nexa-wf-student-question"]
    calc = wf.step("calculate").model_copy(update={"policy": wfm.StepPolicy(timeout_s=1, max_attempts=2,
                                                                             backoff_s=0)})
    monkeypatch.setitem(engine.WORKFLOWS, wf.id, wf.model_copy(update={
        "steps": [calc if s.id == "calculate" else s for s in wf.steps],
        "budget": wfm.Budget(max_steps=6, max_agent_calls=4, max_wall_s=60)}))
    t0 = time.time()
    r = start("nexa-wf-student-question", STUDENT, {"question": "Calculate 2 + 2"})
    assert r["status"] == "failed" and "calculate" in r["error"]
    assert time.time() - t0 < 4.5                                   # 2 x 1 s per agent, 2 agents
    assert [kk for _, kk in kinds(r)].count("step.attempt_failed") == 4   # precise x2, then tutor x2


def test_access_and_input_rules(monkeypatch, gw):
    knowledge(monkeypatch)
    with pytest.raises(engine.OrchestrationError) as e:
        start("nexa-wf-lesson-explainer", STUDENT, {"topic": "Fractions", "grade": "11"})
    assert e.value.status_code == 403
    with pytest.raises(engine.OrchestrationError) as e:
        start("nexa-wf-student-question", STUDENT, {"question": "", "extra": "x"})
    assert e.value.status_code == 422
    with pytest.raises(engine.OrchestrationError) as e:
        start("nexa-wf-nope", STUDENT, {"question": "hi"})
    assert e.value.status_code == 404
    wf = engine.WORKFLOWS["nexa-wf-student-question"]
    monkeypatch.setitem(engine.WORKFLOWS, wf.id, wf.model_copy(update={"state": "draft"}))
    with pytest.raises(engine.OrchestrationError) as e:
        start("nexa-wf-student-question", STUDENT, {"question": "hi"})
    assert e.value.status_code == 409


# ---------------------------------------------------------------- bounded multi-agent workflow

LESSON = {"topic": "Simple interest", "grade": "11"}


def test_bounded_multi_agent_with_human_checkpoint(monkeypatch):
    g = Gateway(choices=[{"action": "explain", "task": "Explain simple interest."},
                         {"action": "worked_example", "task": "Work one example: K500 at 4% for 3 years."}])
    monkeypatch.setattr(gateway_client, "chat", g.chat)
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    assert r["status"] == "waiting_approval" and r["checkpoint"]["step"] == "review"
    assert "## Explain" in r["checkpoint"]["show"] and "## Worked example" in r["checkpoint"]["show"]
    # The engine made every call: coordinator, delegate, coordinator, delegate. Limit 2 reached.
    roles = [t["detail"]["role"] for t in engine.trace_of(r["run_id"]) if t["kind"] == "agent.call"]
    assert roles == ["coordinator", "delegate", "coordinator", "delegate"]
    ch = [t["detail"] for t in engine.trace_of(r["run_id"]) if t["kind"] == "delegate.choice"]
    assert [(c["action"], c["accepted"]) for c in ch] == [("explain", True), ("worked_example", True)]
    fin = next(t for t in engine.trace_of(r["run_id"]) if t["kind"] == "delegate.finished")
    assert fin["detail"] == {"delegations": 2, "decisions": 2, "reason": "limit"}
    assert len(g.calls) == 4 and r["counters"]["agent_calls"] == 4
    # Human in the loop: four eyes, roles.
    for who, code in ((TEACHER, 403), (STUDENT, 403)):
        with pytest.raises(engine.OrchestrationError) as e:
            asyncio.run(engine.decide(r["run_id"], who, "approve"))
        assert e.value.status_code == code
    done = asyncio.run(engine.decide(r["run_id"], TEACHER2, "approve", "Good for Monday."))
    assert done["status"] == "succeeded"
    assert "Reviewed and approved by user:teacher-2. Good for Monday." in done["output"]
    assert "## Worked example" in done["output"]
    k = [kk for _, kk in kinds(done)]
    assert k.count("checkpoint.refused") == 2 and "checkpoint.decided" in k and k[-1] == "run.succeeded"
    with pytest.raises(engine.OrchestrationError) as e:            # decided once only
        asyncio.run(engine.decide(r["run_id"], TEACHER2, "approve"))
    assert e.value.status_code == 409


def test_coordinator_cannot_leave_the_allowlist(monkeypatch):
    g = Gateway(choices=[{"action": "explain", "task": "Explain it."},
                         {"action": "nexa-lesson-planner", "task": "Write the whole unit plan."}])
    monkeypatch.setattr(gateway_client, "chat", g.chat)
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    ch = [t["detail"] for t in engine.trace_of(r["run_id"]) if t["kind"] == "delegate.choice"]
    assert ch[1]["accepted"] is False and "not allowed" in ch[1]["problem"]
    assert r["status"] == "waiting_approval"                       # the engine stops delegating, keeps the work
    assert len(g.calls) == 3 and not any("unit plan" in c["prompt"] for c in g.calls[2:])
    fin = next(t for t in engine.trace_of(r["run_id"]) if t["kind"] == "delegate.finished")
    assert fin["detail"]["reason"].startswith("invalid choice")


def test_coordinator_nonsense_fails_the_step_and_done_stops_early(monkeypatch):
    monkeypatch.setattr(gateway_client, "chat", Gateway(choices=["I think you should explain it."]).chat)
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    assert r["status"] == "failed" and "no work was delegated" in r["error"]
    monkeypatch.setattr(gateway_client, "chat", Gateway(choices=[{"action": "explain", "task": "Explain."},
                                                                  {"action": "done"}]).chat)
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    assert r["status"] == "waiting_approval" and r["counters"]["agent_calls"] == 3


def test_reject_ends_the_run(monkeypatch):
    monkeypatch.setattr(gateway_client, "chat", Gateway(choices=[{"action": "explain", "task": "Explain."}]).chat)
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    out = asyncio.run(engine.decide(r["run_id"], TEACHER2, "reject", "Too advanced."))
    assert out["status"] == "rejected" and out["output"] is None


def test_budget_bounds_the_multi_agent_run(monkeypatch):
    monkeypatch.setattr(gateway_client, "chat", Gateway(choices=[{"action": "explain", "task": "a"},
                                                                  {"action": "explain", "task": "b"}]).chat)
    wf = engine.WORKFLOWS["nexa-wf-lesson-explainer"]
    monkeypatch.setitem(engine.WORKFLOWS, wf.id, wf.model_copy(update={
        "budget": wfm.Budget(max_steps=5, max_agent_calls=3, max_wall_s=60)}))
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    assert r["status"] == "budget_exceeded" and "max_agent_calls 3" in r["error"]
    assert r["counters"]["agent_calls"] == 3


def test_checkpoint_expiry(monkeypatch):
    monkeypatch.setattr(gateway_client, "chat", Gateway(choices=[{"action": "explain", "task": "Explain."}]).chat)
    r = start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    registry.REGISTRY.store.query("UPDATE workflow_runs SET waiting_until = ? WHERE run_id = ?",
                                  (time.time() - 1, r["run_id"]))
    with pytest.raises(engine.OrchestrationError):
        asyncio.run(engine.decide(r["run_id"], TEACHER2, "approve"))
    assert engine.load_run(r["run_id"]).status == "expired"


# ---------------------------------------------------------------- no uncontrolled agent-to-agent calls

def test_an_agent_cannot_call_another_agent(monkeypatch):
    seen = {}

    async def sneaky(model, messages, temperature=None, max_tokens=None, trace_id=None):
        # Something running inside nexa-tutor's run tries to run another agent,
        # and to start a workflow.
        try:
            await runtime.run_agent("nexa-precise", STUDENT, [{"role": "user", "content": "hi"}])
        except runtime.AgentRuntimeError as exc:
            seen["agent"] = (exc.status_code, exc.detail)
        try:
            await engine.start("nexa-wf-student-question", STUDENT, {"question": "x"})
        except engine.OrchestrationError as exc:
            seen["workflow"] = exc.status_code
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(gateway_client, "chat", sneaky)
    out = asyncio.run(runtime.run_agent("nexa-tutor", STUDENT, [{"role": "user", "content": "hi"}]))
    assert out.output == "ok"
    assert seen["agent"][0] == 409 and "orchestration engine" in seen["agent"][1]
    assert seen["workflow"] == 409


def test_delegate_agents_never_see_each_other(monkeypatch):
    """Agents are called one at a time by the engine; no call happens inside another."""
    depth = {"now": 0, "max": 0}
    g = Gateway(choices=[{"action": "explain", "task": "Explain."}, {"action": "worked_example", "task": "One."}])

    async def chat(*a, **k):
        depth["now"] += 1
        depth["max"] = max(depth["max"], depth["now"])
        try:
            assert runtime.active_run() is not None
            return await g.chat(*a, **k)
        finally:
            depth["now"] -= 1
    monkeypatch.setattr(gateway_client, "chat", chat)
    start("nexa-wf-lesson-explainer", TEACHER, LESSON)
    assert depth["max"] == 1 and runtime.active_run() is None


# ---------------------------------------------------------------- workflow contract

def raw(name):
    with open(f"{wfm.WORKFLOWS_DIR}/{name}.json") as fh:
        return json.load(fh)


def bad(raw_wf):
    with pytest.raises(Exception) as e:
        wfm.Workflow.model_validate(raw_wf)
    return str(e.value)


def test_workflow_contract_rejects_unsafe_graphs():
    r = raw("nexa-wf-student-question")
    r["steps"][4]["next"] = "route"
    assert "cycle" in bad(r)
    r = raw("nexa-wf-lesson-explainer")
    r["steps"][2] = {"id": "release", "type": "knowledge", "title": "Late lookup", "question": "{{input.topic}}"}
    assert "after a checkpoint" in bad(r)
    r = raw("nexa-wf-lesson-explainer")
    r["pattern"] = "bounded_multi_agent"
    r["steps"][0] = {"id": "prepare", "type": "agent", "title": "One agent", "agent": "nexa-tutor",
                     "prompt": "x {{input.topic}}", "next": "review"}
    assert "delegate step" in bad(r)
    r = raw("nexa-wf-student-question")
    r["steps"][3]["prompt"] = "{{input.secret}}"
    assert "not an input field" in bad(r)
    r = raw("nexa-wf-lesson-explainer")
    r["steps"][0]["max_delegations"] = 9
    assert "max_delegations" in bad(r)


def test_workflow_platform_checks():
    from v1.identity.rbac import ROLE_PERMISSIONS
    r = raw("nexa-wf-lesson-explainer")
    r["steps"][0]["delegates"]["plan"] = "nexa-unknown"
    wf = wfm.Workflow.model_validate(r)
    assert any("nexa-unknown" in p for p in wfm.validate_platform(wf, set(registry.AGENTS), ROLE_PERMISSIONS))
    assert {"nexa-wf-student-question", "nexa-wf-lesson-explainer"} <= set(engine.WORKFLOWS)


def test_published_workflow_schema_matches():
    with open(f"{wfm.WORKFLOWS_DIR}/../schema/nexa-workflow-1.0.schema.json") as fh:
        assert json.load(fh) == wfm.json_schema()


def test_restart_marks_running_runs_interrupted(monkeypatch, gw):
    knowledge(monkeypatch)
    r = start("nexa-wf-student-question", STUDENT, {"question": "x"})
    registry.REGISTRY.store.query("UPDATE workflow_runs SET status = 'running' WHERE run_id = ?", (r["run_id"],))
    assert engine.mark_interrupted() == 1 and engine.load_run(r["run_id"]).status == "interrupted"


# ---------------------------------------------------------------- API

def hdr(p):
    return {"Authorization": "Bearer " + tokens.issue_token(p.subject, list(p.roles), "user", 300)}


def test_api_end_to_end_with_background_run_and_decision(monkeypatch):
    from v1.agent_runtime.main import app
    monkeypatch.setattr(gateway_client, "chat", Gateway(choices=[{"action": "explain", "task": "Explain."},
                                                                  {"action": "done"}]).chat)
    with TestClient(app) as c:
        assert {"nexa-wf-student-question", "nexa-wf-lesson-explainer"} <= \
            {w["id"] for w in c.get("/v1/workflows", headers=hdr(TEACHER)).json()["workflows"]}
        r = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs?wait=false", json={"input": LESSON},
                   headers=hdr(TEACHER))
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        for _ in range(100):
            body = c.get(f"/v1/workflow-runs/{run_id}", headers=hdr(TEACHER)).json()
            if body["status"] != "running":
                break
            time.sleep(0.05)
        assert body["status"] == "waiting_approval" and body["trace"][0]["kind"] == "run.started"
        queue = c.get("/v1/workflow-runs?status=waiting_approval", headers=hdr(TEACHER2)).json()["runs"]
        assert run_id in {x["run_id"] for x in queue}
        assert c.get(f"/v1/workflow-runs/{run_id}", headers=hdr(STUDENT)).status_code == 404
        r = c.post(f"/v1/workflow-runs/{run_id}/decision", json={"decision": "approve"}, headers=hdr(TEACHER))
        assert r.status_code == 403
        r = c.post(f"/v1/workflow-runs/{run_id}/decision", json={"decision": "approve", "comment": "ok"},
                   headers=hdr(TEACHER2))
        assert r.status_code == 200 and r.json()["status"] == "succeeded"
        assert c.get("/health").json()["workflows"]["nexa-wf-lesson-explainer"]["pattern"] == "bounded_multi_agent"


def test_api_gateway_forwards_workflow_calls(monkeypatch):
    from v1.agent_runtime.main import app
    from v1.api_gateway import main as api
    knowledge(monkeypatch)
    monkeypatch.setattr(gateway_client, "chat", Gateway().chat)
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=app))
    gw = TestClient(api.app)
    assert gw.get("/v1/workflows", headers=hdr(STUDENT)).status_code == 200
    r = gw.post("/v1/workflows/nexa-wf-student-question/runs", json={"input": {"question": "What is osmosis?"}},
                headers=hdr(STUDENT))
    assert r.status_code == 201 and r.json()["status"] == "succeeded"
    run = gw.get(f"/v1/workflow-runs/{r.json()['run_id']}", headers=hdr(STUDENT)).json()
    assert run["trace"][-1]["kind"] == "run.succeeded"
    assert gw.post("/v1/workflows/nexa-wf-lesson-explainer/runs", json={"input": LESSON},
                   headers=hdr(STUDENT)).status_code == 403
    assert gw.get("/v1/workflows").status_code == 401
