"""NEXA-M3-005: the inter-agent message and event contract (nexa.message/1.0).
Versioned schemas, validated in a multi-agent workflow; correlation, causation
and trace ids; identities; permissions; deadlines, retries, idempotency and
failure semantics; no agent-to-agent messages."""
import asyncio
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import gateway_client, presence, registry
from v1.agent_runtime.messaging import bus, contract as mc
from v1.agent_runtime.orchestration import engine
from v1.identity import tokens
from v1.identity.principal import Principal

TEACHER = Principal("user:teacher-1", ("teacher",), "user")
TEACHER2 = Principal("user:teacher-2", ("teacher",), "user")
LESSON = {"topic": "Simple interest", "grade": "11"}
ORCH = engine.ORCHESTRATOR


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


def scripted(monkeypatch, choices, fail=None):
    calls = []

    async def chat(model, messages, temperature=None, max_tokens=None, trace_id=None):
        text = messages[-1]["content"]
        calls.append(text)
        if fail and fail(len(calls)):
            raise gateway_client.GatewayError("gateway 503", "unavailable")
        if "Reply with only one JSON object" in text:
            c = choices.pop(0) if choices else {"action": "done"}
            return {"choices": [{"message": {"content": json.dumps(c)}}]}
        return {"choices": [{"message": {"content": f"[{model}] work"}}]}
    monkeypatch.setattr(gateway_client, "chat", chat)
    return calls


def msgs(run_id):
    return [row["message"] for row in bus.conversation(run_id)]


# ---------------------------------------------------------------- the multi-agent workflow, validated

def test_multi_agent_conversation_follows_the_contract(monkeypatch):
    scripted(monkeypatch, [{"action": "explain", "task": "Explain it."},
                           {"action": "worked_example", "task": "One example."}])
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", TEACHER, LESSON))
    done = asyncio.run(engine.decide(r["run_id"], TEACHER2, "approve", "ok"))
    assert done["status"] == "succeeded"
    check = bus.check_conversation(r["run_id"])
    assert check["valid"], check["problems"]
    assert check["counts"]["request"] == 4 and check["counts"]["result"] == 4 and check["counts"]["event"] > 10

    all_m = msgs(r["run_id"])
    by_id = {m["message_id"]: m for m in all_m}
    for m in all_m:
        mc.Message.model_validate(m)                                    # every envelope and payload
        assert m["schema"] == "nexa.message/1.0"
        assert m["correlation_id"] == r["run_id"] and m["trace_id"] == r["trace_id"]
        assert not (m["sender"]["kind"] == "agent" and (m.get("receiver") or {}).get("kind") == "agent")
    reqs = [m for m in all_m if m["kind"] == "request"]
    assert [(q["receiver"]["id"], q["payload"]["role"]) for q in reqs] == [
        ("nexa-precise", "coordinator"), ("nexa-tutor", "delegate"),
        ("nexa-precise", "coordinator"), ("nexa-precise", "delegate")]
    for q in reqs:
        assert q["sender"] == ORCH.model_dump(exclude_none=True)
        assert q["principal"] == {"subject": "user:teacher-1", "roles": ["teacher"]}
        assert q["permission"] == "model.invoke" and q["attempt"] == 1 and q["deadline"] > q["created_at"]
        assert q["payload_schema"] == "nexa.payload.agent.task/1"
    results = [m for m in all_m if m["kind"] == "result"]
    for res in results:
        req = by_id[res["in_reply_to"]]
        assert res["sender"]["id"] == req["receiver"]["id"] and res["receiver"]["kind"] == "orchestrator"
        assert res["sender"]["version"] == "1.0.0" and res["payload"]["latency_ms"] >= 0
    # Delegation: coordinator result -> orchestrator's decision (event) -> delegate request.
    delegate_req = reqs[1]
    decision = by_id[delegate_req["causation_id"]]
    assert decision["type"] == "delegation.choice" and decision["sender"]["kind"] == "orchestrator"
    assert decision["payload"] == {"step": "prepare", "decision": 1, "proposed": decision["payload"]["proposed"],
                                   "accepted": True, "action": "explain", "delegate": "nexa-tutor"}
    coord_result = by_id[decision["causation_id"]]
    assert coord_result["kind"] == "result" and coord_result["sender"]["id"] == "nexa-precise"
    # Every causation chain reaches the run's first event.
    first = all_m[0]
    assert first["type"] == "run.started" and "causation_id" not in first
    for m in all_m[1:]:
        hops, cur = 0, m
        while cur.get("causation_id"):
            cur, hops = by_id[cur["causation_id"]], hops + 1
            assert hops < 500
        assert cur["message_id"] == first["message_id"]
    # Events carry the trace entries, in order.
    evs = [m for m in all_m if m["kind"] == "event" and m["type"] != "delegation.choice"]
    assert [e["payload"]["seq"] for e in evs] == sorted(e["payload"]["seq"] for e in evs)
    assert evs[-1]["type"] == "run.succeeded"


def test_retry_is_a_new_attempt_under_the_same_idempotency_key(monkeypatch):
    scripted(monkeypatch, [{"action": "explain", "task": "Explain."}], fail=lambda n: n <= 2)   # the runtime retries once itself
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", TEACHER, LESSON))
    assert r["status"] == "waiting_approval"
    all_m = msgs(r["run_id"])
    reqs = [m for m in all_m if m["kind"] == "request"]
    first, second = reqs[0], reqs[1]
    assert first["idempotency_key"] == second["idempotency_key"] and (first["attempt"], second["attempt"]) == (1, 2)
    err = next(m for m in all_m if m["kind"] == "error")
    assert err["in_reply_to"] == first["message_id"] and err["type"] == "agent.error"
    assert err["error"]["code"] == "unavailable" and err["error"]["retryable"] is True and err["error"]["status"] == 502
    assert bus.check_conversation(r["run_id"])["valid"]


def test_refused_is_final_no_retry(monkeypatch):
    scripted(monkeypatch, [])
    registry.REGISTRY.set_enabled("nexa-precise", False, "user:admin", "drill")
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", TEACHER, LESSON))
    assert r["status"] == "failed"
    all_m = msgs(r["run_id"])
    assert len([m for m in all_m if m["kind"] == "request"]) == 1
    err = next(m for m in all_m if m["kind"] == "error")
    assert err["error"]["code"] == "refused" and err["error"]["retryable"] is False and err["error"]["status"] == 423


# ---------------------------------------------------------------- the bus

def _req(key="run_test0001/s/agent/x", payload=None, deadline=None, **kw):
    return mc.make("request", "agent.task", correlation_id="run_test0001", trace_id="trace_0001", sender=ORCH,
                   receiver=mc.Party(kind="agent", id="nexa-tutor", version="1.0.0"),
                   payload=payload or {"role": "agent", "prompt": "hi"},
                   principal=mc.OnBehalfOf(subject="user:t", roles=["teacher"]), permission="model.invoke",
                   deadline=deadline or time.time() + 5, timeout_s=5, attempt=1, max_attempts=2,
                   idempotency_key=key, **kw)


def test_idempotency_done_once_and_conflicts_refused():
    calls = []

    async def handler(m, left):
        calls.append(m.message_id)
        return bus.Delivered(payload={"output": "x", "latency_ms": 1}, sender_version="1.0.0")
    a = asyncio.run(bus.request(_req(), handler))
    b = asyncio.run(bus.request(_req(), handler))
    assert a.kind == b.kind == "result" and b.payload == a.payload and len(calls) == 1
    assert bus.conversation("run_test0001")[-1]["status"] == "duplicate"
    c = asyncio.run(bus.request(_req(payload={"role": "agent", "prompt": "something else"}), handler))
    assert c.kind == "error" and c.error.code == "conflict" and not c.error.retryable and len(calls) == 1


def test_deadlines():
    async def slow(m, left):
        await asyncio.sleep(2)
        return bus.Delivered(payload={"output": "late", "latency_ms": 1})
    out = asyncio.run(bus.request(_req(key="run_test0001/s/agent/slow", deadline=time.time() + 0.3), slow))
    assert out.kind == "error" and out.error.code == "timeout" and out.error.retryable
    out = asyncio.run(bus.request(_req(key="run_test0001/s/agent/old", deadline=time.time() - 1), slow))
    assert out.kind == "error" and out.error.code == "timeout" and not out.error.retryable
    assert [r["status"] for r in bus.conversation("run_test0001")][-2:] == ["expired", "expired"]


AGENT = mc.Party(kind="agent", id="nexa-tutor", version="1.0.0")
OTHER = mc.Party(kind="agent", id="nexa-precise", version="1.0.0")


@pytest.mark.parametrize("build, problem", [
    (lambda: mc.make("request", "agent.task", correlation_id="run_x00001", trace_id="trace_0001", sender=AGENT,
                     receiver=OTHER, payload={"role": "agent", "prompt": "do my work"},
                     principal=mc.OnBehalfOf(subject="u:1", roles=[]), permission="model.invoke",
                     deadline=time.time() + 5, timeout_s=5, attempt=1, max_attempts=1,
                     idempotency_key="k-000000001"), "only the orchestrator sends requests"),
    (lambda: mc.make("result", "agent.result", correlation_id="run_x00001", trace_id="trace_0001", sender=AGENT,
                     receiver=OTHER, in_reply_to="msg_000000000000", payload={"output": "x", "latency_ms": 1}),
     "agents never message agents"),
    (lambda: mc.make("event", "run.started", correlation_id="run_x00001", trace_id="trace_0001", sender=ORCH,
                     receiver=AGENT, payload={"workflow": "w", "run_id": "r", "seq": 1}), "an event has no receiver"),
    (lambda: mc.make("event", "run.started", correlation_id="run_x00001", trace_id="trace_0001", sender=AGENT,
                     payload={"workflow": "w", "run_id": "r", "seq": 1}), "agents do not send events"),
    (lambda: mc.make("request", "agent.task", correlation_id="run_x00001", trace_id="trace_0001", sender=ORCH,
                     receiver=AGENT, payload={"role": "agent", "prompt": "x"}), "a request carries deadline"),
    (lambda: mc.make("request", "agent.task", correlation_id="run_x00001", trace_id="trace_0001", sender=ORCH,
                     receiver=AGENT, payload={"role": "boss", "prompt": "x"},
                     principal=mc.OnBehalfOf(subject="u:1", roles=[]), permission="model.invoke",
                     deadline=time.time() + 5, timeout_s=5, attempt=1, max_attempts=1,
                     idempotency_key="k-000000001"), "payload.role"),
    (lambda: mc.make("error", "agent.error", correlation_id="run_x00001", trace_id="trace_0001", sender=AGENT,
                     receiver=ORCH, in_reply_to="msg_000000000000",
                     error={"code": "refused", "retryable": True, "reason": "no"}), "never retryable"),
    (lambda: mc.make("result", "agent.result", correlation_id="run_x00001", trace_id="trace_0001", sender=AGENT,
                     receiver=ORCH, payload={"output": "x", "latency_ms": 1}), "in_reply_to"),
])
def test_envelope_rules(build, problem):
    with pytest.raises(Exception, match=problem):
        build()


def test_schema_version_is_fixed_and_published():
    raw = _req().dump()
    raw["schema"] = "nexa.message/2.0"
    with pytest.raises(Exception):
        mc.Message.model_validate(raw)
    path = os.path.join(os.path.dirname(mc.__file__), "..", "schema", "nexa-message-1.0.schema.json")
    with open(path) as fh:
        assert json.load(fh) == mc.json_schema()
    s = mc.json_schema()
    assert set(s["x-nexa-payloads"]) >= {"nexa.payload.agent.task/1", "nexa.payload.agent.result/1",
                                         "nexa.payload.tool.task/1", "nexa.payload.delegation.choice/1"}
    assert s["x-nexa-error-codes"]["retryable"] == ["timeout", "unavailable"]


# ---------------------------------------------------------------- API

def hdr(p):
    return {"Authorization": "Bearer " + tokens.issue_token(p.subject, list(p.roles), "user", 300)}


def test_api_messages_and_idempotent_start(monkeypatch):
    from v1.agent_runtime.main import app
    scripted(monkeypatch, [{"action": "explain", "task": "Explain."}, {"action": "done"}])
    c = TestClient(app)
    h = hdr(TEACHER) | {"Idempotency-Key": "lesson-2026-11-18-a"}
    a = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs", json={"input": LESSON}, headers=h)
    b = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs", json={"input": LESSON}, headers=h)
    assert a.status_code == 201 and b.status_code == 200 and b.json()["duplicate"]
    assert a.json()["run_id"] == b.json()["run_id"]
    d = c.post("/v1/workflows/nexa-wf-lesson-explainer/runs", json={"input": {**LESSON, "grade": "12"}}, headers=h)
    assert d.status_code == 409
    m = c.get(f"/v1/workflow-runs/{a.json()['run_id']}/messages", headers=hdr(TEACHER)).json()
    assert m["check"]["valid"] and m["messages"][0]["message"]["type"] == "run.started"
    assert c.get("/v1/messaging/schema", headers=hdr(TEACHER)).json()["title"].startswith("NEXA message envelope")
    assert c.get(f"/v1/workflow-runs/{a.json()['run_id']}/messages",
                 headers=hdr(Principal("user:s", ("student",)))).status_code == 404


def test_api_gateway_forwards_the_idempotency_key(monkeypatch):
    import httpx

    from v1.agent_runtime.main import app
    from v1.api_gateway import main as api
    scripted(monkeypatch, [{"action": "explain", "task": "Explain."}, {"action": "done"}])
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=app))
    gw = TestClient(api.app)
    h = hdr(TEACHER) | {"Idempotency-Key": "via-gateway-0001"}
    a = gw.post("/v1/workflows/nexa-wf-lesson-explainer/runs", json={"input": LESSON}, headers=h).json()
    b = gw.post("/v1/workflows/nexa-wf-lesson-explainer/runs", json={"input": LESSON}, headers=h).json()
    assert a["run_id"] == b["run_id"] and b["duplicate"]
    assert gw.get("/v1/messaging/schema", headers=hdr(TEACHER)).status_code == 200


def test_live_check_tool_passes_against_the_real_apps(monkeypatch, capsys):
    """v1/tools/messaging_check.py, run against the API Gateway and the Agent
    Runtime in process (only the model is scripted)."""
    import httpx

    from v1.agent_runtime.main import app
    from v1.api_gateway import main as api
    from v1.tools import messaging_check
    scripted(monkeypatch, [{"action": "explain", "task": "Explain."}, {"action": "worked_example", "task": "One."}])
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=app))
    with TestClient(api.app) as gw:
        monkeypatch.setattr(messaging_check.httpx, "Client", lambda base_url, timeout: gw)
        monkeypatch.setattr(messaging_check.time, "sleep", lambda s: time.sleep(0.05))
        code = messaging_check.main([])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "checks passed" in out and "FAIL" not in out
