"""NEXA-M3-006: agent state, session and memory handoff. Multi-step workflows
resume safely; state access is policy-controlled and auditable; session state
is short-lived and never holds learner records, personal data or credentials."""
import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import gateway_client, presence, registry
from v1.agent_runtime.messaging import bus
from v1.agent_runtime.orchestration import engine
from v1.agent_runtime.state import contract as sc, purge, store as state
from v1.identity import tokens
from v1.identity.principal import Principal

TEACHER = Principal("user:teacher-1", ("teacher",), "user")
TEACHER2 = Principal("user:teacher-2", ("teacher",), "user")
ADMIN = Principal("user:admin-1", ("admin",), "user")
AUDITOR = Principal("user:auditor-1", ("auditor",), "user")
LESSON = {"topic": "Simple interest", "grade": "11"}
WF = "nexa-wf-lesson-explainer"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    registry.reload(":memory:")
    engine.load_workflows()

    async def deps():
        return {"model_gateway": {"ok": True}, "knowledge_service": {"ok": True}, "tool_gateway": {"ok": True},
                "memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", deps)
    asyncio.run(presence.beat())
    yield
    registry.reload(":memory:")
    engine.load_workflows()


class Model:
    def __init__(self, choices, block_on=None):
        self.choices, self.block_on, self.calls = list(choices), block_on, []
        self.gate = None

    async def chat(self, model, messages, temperature=None, max_tokens=None, trace_id=None):
        text = messages[-1]["content"]
        self.calls.append(text)
        if self.block_on and len(self.calls) == self.block_on:
            await asyncio.Event().wait()        # never returns: the runtime "crashes" here
        if "Reply with only one JSON object" in text:
            done_so_far = text.count("\nTask: ")              # work already shown to the coordinator
            c = self.choices[done_so_far] if done_so_far < len(self.choices) else {"action": "done"}
            return {"choices": [{"message": {"content": json.dumps(c)}}]}
        return {"choices": [{"message": {"content": f"[{model}] work {len(self.calls)}"}}]}


CHOICES = [{"action": "explain", "task": "Explain it."}, {"action": "worked_example", "task": "One example."}]


# ---------------------------------------------------------------- checkpoints and safe resume

def test_every_step_is_checkpointed_without_credentials(monkeypatch):
    m = Model(CHOICES)
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    tok = tokens.issue_token("user:teacher-1", ["teacher"], "user", 300)
    r = asyncio.run(engine.start(WF, TEACHER, LESSON, auth=f"Bearer {tok}"))
    cps = state.checkpoints(r["run_id"])
    assert [c["version"] for c in cps] == sorted(c["version"] for c in cps) and all(c["valid"] for c in cps)
    assert cps[-1]["status"] == "waiting_approval" and cps[-1]["step"] == "review"
    raw = json.dumps([dict(x) for x in registry.REGISTRY.store.query("SELECT data FROM run_checkpoints")])
    assert tok not in raw and "Bearer" not in raw


def test_interrupted_multi_agent_run_resumes_without_redoing_work(monkeypatch):
    m = Model(CHOICES, block_on=3)                      # crash during the second coordinator call
    monkeypatch.setattr(gateway_client, "chat", m.chat)

    async def crash_and_restart():
        r = await engine.start(WF, TEACHER, LESSON, wait=False)
        for _ in range(200):
            if len(m.calls) >= 3:
                break
            await asyncio.sleep(0.01)
        for t in list(engine._TASKS):                    # the process dies
            t.cancel()
        await asyncio.sleep(0.05)
        return r["run_id"]
    run_id = asyncio.run(crash_and_restart())
    assert engine.load_run(run_id).status == "running"
    assert engine.mark_interrupted() == 1               # the runtime starts again
    v = engine.view(engine.load_run(run_id))
    assert v["status"] == "interrupted" and v["resumable"] is True

    with pytest.raises(engine.OrchestrationError) as e:  # only the person who started it
        asyncio.run(engine.resume(run_id, TEACHER2))
    assert e.value.status_code == 404
    m.block_on = None
    out = asyncio.run(engine.resume(run_id, TEACHER))
    assert out["status"] == "waiting_approval"
    # 2 calls done before the crash, 1 lost in the crash, then only the 2 not finished.
    assert len(m.calls) == 5
    statuses = [(row["message"]["kind"], row["status"]) for row in bus.conversation(run_id)
                if row["message"]["kind"] in ("request", "result")]
    assert statuses.count(("request", "duplicate")) == 2 and statuses.count(("result", "duplicate")) == 2
    assert bus.check_conversation(run_id)["valid"]
    kinds = [t["kind"] for t in engine.trace_of(run_id)]
    assert "run.interrupted" in kinds and "run.resumed" in kinds
    done = asyncio.run(engine.decide(run_id, TEACHER2, "approve"))
    assert done["status"] == "succeeded" and "## Worked example" in done["output"]
    acts = [(a["action"], a["decision"], a["actor"]) for a in state.access_log(target=run_id)]
    assert ("resume", "allow", "user:teacher-1") in acts and ("resume", "deny", "user:teacher-2") in acts


def test_resume_refused_on_a_changed_workflow_or_a_damaged_checkpoint(monkeypatch):
    m = Model(CHOICES, block_on=1)
    monkeypatch.setattr(gateway_client, "chat", m.chat)

    async def crash():
        r = await engine.start(WF, TEACHER, LESSON, wait=False)
        await asyncio.sleep(0.05)
        for t in list(engine._TASKS):
            t.cancel()
        await asyncio.sleep(0.02)
        return r["run_id"]
    run_id = asyncio.run(crash())
    engine.mark_interrupted()
    wf = engine.WORKFLOWS[WF]
    monkeypatch.setitem(engine.WORKFLOWS, WF, wf.model_copy(update={"description": wf.description + " Changed."}))
    with pytest.raises(engine.OrchestrationError) as e:
        asyncio.run(engine.resume(run_id, TEACHER))
    assert e.value.status_code == 409 and "workflow changed" in e.value.detail
    monkeypatch.setitem(engine.WORKFLOWS, WF, wf)
    registry.REGISTRY.store.query("UPDATE run_checkpoints SET data = replace(data, 'prepare', 'tampered') "
                                  "WHERE run_id = ?", (run_id,))
    with pytest.raises(engine.OrchestrationError) as e:
        asyncio.run(engine.resume(run_id, TEACHER))
    assert e.value.status_code == 409 and "no valid checkpoint" in e.value.detail


# ---------------------------------------------------------------- session state and handoff

def test_session_hands_context_from_one_run_to_the_next(monkeypatch):
    m = Model(CHOICES)
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    ses = state.create_session(TEACHER, [WF], 3600, known_workflows=set(engine.WORKFLOWS))
    r1 = asyncio.run(engine.start(WF, TEACHER, LESSON, session_id=ses.id))
    assert asyncio.run(engine.decide(r1["run_id"], TEACHER2, "approve"))["status"] == "succeeded"
    assert "session.written" in [t["kind"] for t in engine.trace_of(r1["run_id"])]
    rec = state.read_session(ses.id, TEACHER)
    assert rec.version == 2 and rec.data == {"last_topic": "Simple interest", "last_grade": "11",
                                             "last_reviewed_by": "user:teacher-2"}
    m.calls.clear()
    m.choices = list(CHOICES)
    asyncio.run(engine.start(WF, TEACHER, {"topic": "Compound interest", "grade": "11"}, session_id=ses.id))
    assert 'Earlier in this teacher\'s session (may be empty): topic "Simple interest", Grade 11' in m.calls[0]
    log = state.access_log(target=ses.id)
    assert [(a["action"], a["via"], a["decision"]) for a in reversed(log)][:4] == [
        ("create", None, "allow"), ("read", WF, "allow"), ("write", WF, "allow"), ("read", None, "allow")]
    reads = [a for a in log if a["action"] == "read" and a["via"] == WF]
    assert all(set(a["keys"]) <= {"last_topic", "last_grade"} for a in reads)   # only declared keys


def test_session_policy(monkeypatch):
    ses = state.create_session(TEACHER, [WF], 3600, known_workflows=set(engine.WORKFLOWS))
    with pytest.raises(sc.StateError) as e:
        state.read_session(ses.id, TEACHER2)
    assert e.value.status_code == 404                                     # not even its existence
    meta = state.session_metadata(ses.id, ADMIN)
    assert meta["owner"] == "user:teacher-1" and "data" not in meta
    with pytest.raises(sc.StateError) as e:
        state.read_session(ses.id, ADMIN)                                 # admins see metadata, never values
    assert e.value.status_code == 403
    with pytest.raises(engine.OrchestrationError) as e:                   # a workflow the owner did not name
        asyncio.run(engine.start("nexa-wf-student-question", TEACHER, {"question": "hi"}, session_id=ses.id))
    assert e.value.status_code == 422                                     # it uses no session at all
    for bad, why in (({"marks": 72}, "learner records"), ({"note": "write to kila@school.pg"}, "personal data"),
                     ({"note": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXRlc3Q"}, "credential"),
                     ({"profile": {"$memory": {"service": "nexa-memory", "scope": "learner_profile",
                                               "subject": "user:s", "key": "p"}}}, "memory scope"),
                     ({"Bad-Key": 1}, "lower snake case")):
        with pytest.raises(sc.StateError) as e:
            state.write_session(ses.id, TEACHER, bad, expected_version=1)
        assert e.value.status_code == 422 and why in json.dumps(e.value.detail), bad
    state.write_session(ses.id, TEACHER, {"note": "fractions next"}, expected_version=1)
    with pytest.raises(sc.StateError) as e:
        state.write_session(ses.id, TEACHER, {"note": "stale"}, expected_version=1)
    assert e.value.status_code == 409
    registry.REGISTRY.store.query("UPDATE sessions SET expires_at = ? WHERE session_id = ?", (time.time() - 1, ses.id))
    with pytest.raises(sc.StateError) as e:
        state.read_session(ses.id, TEACHER)
    assert e.value.status_code == 410 and state.load_session_raw(ses.id) is None
    denied = [a for a in state.access_log(target=ses.id) if a["decision"] == "deny"]
    assert len(denied) >= 8


def test_memory_references_are_allowed_only_for_declared_scopes():
    ref = {"profile": {"$memory": {"service": "nexa-memory", "scope": "learner_profile", "subject": "user:s",
                                   "key": "profile"}}}
    assert sc.check_data(ref, "run_checkpoint", memory_scopes=frozenset({"learner_profile"})) == []
    assert sc.check_data(ref, "run_checkpoint")                              # no agent declares a scope today
    copy = {"profile": {"$memory": ref["profile"]["$memory"], "marks": [70, 80]}}
    assert any("nothing but the reference" in p for p in sc.check_data(copy, "session",
                                                                          memory_scopes=frozenset({"learner_profile"})))


# ---------------------------------------------------------------- retention

def test_retention_purges_state_but_keeps_the_audit_record(monkeypatch):
    m = Model([{"action": "explain", "task": "x"}, {"action": "done"}])
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    r = asyncio.run(engine.start(WF, TEACHER, LESSON))
    asyncio.run(engine.decide(r["run_id"], TEACHER2, "reject", "no"))
    ses = state.create_session(TEACHER, [WF], 60, known_workflows=set(engine.WORKFLOWS))
    later = time.time() + 31 * 86400
    assert purge.purge(now=later, dry_run=True) == {"sessions_expired": 1, "runs_purged": 1, "retention_days": 30.0,
                                                     "dry_run": True}
    out = purge.purge(now=later)
    assert out["runs_purged"] == 1 and out["sessions_expired"] == 1
    assert state.checkpoints(r["run_id"]) == [] and state.load_session_raw(ses.id) is None
    run = engine.load_run(r["run_id"])
    assert run.input == {"purged": True} and run.output is None and run.status == "rejected"
    assert engine.trace_of(r["run_id"]) and bus.conversation(r["run_id"])     # the audit record stays
    assert {a["actor"] for a in state.access_log(actor="system:state-retention")} == {"system:state-retention"}


def test_published_state_schema():
    path = os.path.join(os.path.dirname(sc.__file__), "..", "schema", "nexa-state-1.0.schema.json")
    with open(path) as fh:
        assert json.load(fh) == sc.json_schema()


# ---------------------------------------------------------------- API

def hdr(p):
    return {"Authorization": "Bearer " + tokens.issue_token(p.subject, list(p.roles), "user", 300)}


def test_api_through_the_gateway(monkeypatch):
    import httpx

    from v1.agent_runtime.main import app
    from v1.api_gateway import main as api
    m = Model(CHOICES)
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=app))
    gw = TestClient(api.app)
    s = gw.post("/v1/sessions", json={"workflows": [WF], "ttl_s": 3600}, headers=hdr(TEACHER))
    assert s.status_code == 201
    sid = s.json()["id"]
    assert s.json()["schema"] == "nexa.state/1.0" and s.json()["version"] == 1
    r = gw.post(f"/v1/workflows/{WF}/runs", json={"input": LESSON, "session_id": sid}, headers=hdr(TEACHER)).json()
    assert r["status"] == "waiting_approval" and r["session"] == sid
    cps = gw.get(f"/v1/workflow-runs/{r['run_id']}/checkpoints", headers=hdr(TEACHER)).json()
    assert cps["checkpoints"] and all(c["valid"] for c in cps["checkpoints"])
    gw.post(f"/v1/workflow-runs/{r['run_id']}/decision", json={"decision": "approve"}, headers=hdr(TEACHER2))
    mine = gw.get(f"/v1/sessions/{sid}", headers=hdr(TEACHER)).json()
    assert mine["record"]["data"]["last_topic"] == "Simple interest" and mine["record"]["version"] == 2
    assert "record" not in gw.get(f"/v1/sessions/{sid}", headers=hdr(ADMIN)).json()
    assert gw.get(f"/v1/sessions/{sid}", headers=hdr(TEACHER2)).status_code == 404
    w = gw.put(f"/v1/sessions/{sid}/state", json={"data": {"marks": 70}, "expected_version": 2},
               headers=hdr(TEACHER))
    assert w.status_code == 422
    assert gw.get("/v1/state/access", headers=hdr(TEACHER)).status_code == 403
    log = gw.get(f"/v1/state/access?target={sid}", headers=hdr(AUDITOR)).json()["access"]
    assert {a["action"] for a in log} >= {"create", "read", "write", "metadata"}
    assert gw.delete(f"/v1/sessions/{sid}", headers=hdr(TEACHER)).json()["deleted"]


def test_live_check_tool_without_restart(monkeypatch, capsys):
    import httpx

    from v1.agent_runtime.main import app
    from v1.api_gateway import main as api
    from v1.tools import state_check
    m = Model(CHOICES)
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=app))
    with TestClient(api.app) as gw:
        monkeypatch.setattr(state_check.httpx, "Client", lambda base_url, timeout: gw)
        real_sleep = time.sleep
        monkeypatch.setattr(state_check.time, "sleep", lambda s: real_sleep(0.02))
        code = state_check.main(["--no-restart"])
    out = capsys.readouterr().out
    assert code == 0, out
