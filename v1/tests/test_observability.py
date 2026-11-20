"""NEXA-M3-008: agent observability, tracing and audit. Operators can
reconstruct a multi-agent execution from logs and traces, without exposing
secrets or unnecessary learner data."""
import asyncio
import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import config as rt_config, gateway_client, presence, registry, tools
from v1.agent_runtime.main import app as runtime_app
from v1.agent_runtime.messaging import bus, contract as mc
from v1.agent_runtime.orchestration import engine
from v1.agent_runtime.policy import pdp
from v1.agent_runtime.state import purge
from v1.agent_runtime.tracing import api as tracing_api
from v1.identity import tokens
from v1.identity.principal import Principal
from v1.observability.core import metrics
from v1.tool_gateway import connectors, gateway, policy as tg_policy
from v1.tool_gateway.main import app as tool_app

STUDENT = Principal("user:student-kila", ("student",), "user", school_id="pomnsoe")
MARY = Principal("user:teacher-mary", ("teacher",), "user", school_id="pomnsoe")
JOHN = Principal("user:teacher-john", ("teacher",), "user", school_id="pomnsoe")
OTHER = Principal("user:teacher-other", ("teacher",), "user", school_id="kerevat")
OPERATOR = Principal("user:ops-yasaru", ("ai_engineer",), "user")
AUDITOR = Principal("user:auditor-1", ("auditor",), "user")
TOPIC = "Simple interest on market garden loans"
QUESTION = "Why do plant cells need chloroplasts?"


def tok(p):
    return tokens.issue_token(p.subject, list(p.roles), "user", 600,
                              extra={"school_id": p.school_id} if p.school_id else None)


def hdr(p):
    return {"Authorization": f"Bearer {tok(p)}"}


def knowledge(request):
    return httpx.Response(200, json={
        "status": "grounded", "answer": "Chloroplasts hold chlorophyll, which captures light [S1].",
        "confidence": "high", "evidence": {"level": "strong"},
        "citations": [{"marker": "S1", "used": True, "provenance": {"chunk_id": "chunk-bio11-042",
                                                                     "title": "Biology Grade 11 Student Guide",
                                                                     "page_label": "37"}}]})


@pytest.fixture(autouse=True)
def world(monkeypatch):
    registry.reload(":memory:")
    reg = registry.REGISTRY
    reg.set_state("nexa-curriculum-tutor", "0.2.0", "approved", "user:admin", approved_by="Test approver")
    reg.set_state("nexa-curriculum-tutor", "0.2.0", "active", "user:admin")
    pdp.load()
    engine.load_workflows()
    gateway.load(audit_path=":memory:")
    tg_policy.LIMITER.reset()
    metrics.reset()
    monkeypatch.setattr(connectors, "TRANSPORT", httpx.MockTransport(knowledge))
    monkeypatch.setattr(tools, "TRANSPORT", httpx.ASGITransport(app=tool_app))
    monkeypatch.setattr(tracing_api, "TRANSPORT", httpx.ASGITransport(app=tool_app))
    monkeypatch.setattr(rt_config, "GATEWAY_TOKEN", tokens.issue_token("svc:agent-runtime", ["service"], "service",
                                                                       600))

    async def deps():
        return {k: {"ok": True} for k in ("model_gateway", "knowledge_service", "tool_gateway")} | \
            {"memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", deps)
    asyncio.run(presence.beat())
    yield
    registry.reload(":memory:")
    pdp.load()
    engine.load_workflows()
    gateway.load(audit_path=":memory:")


class Model:
    """A scripted Model Gateway that reports usage, like the real one."""

    def __init__(self, fail_first=0):
        self.calls, self.fail_first = 0, fail_first

    async def chat(self, model, messages, temperature=None, max_tokens=None, trace_id=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise gateway_client.GatewayError("gateway 503", "unavailable")
        p = messages[-1]["content"]
        if "Reply with only one JSON object" in p:
            n = p.count("\nTask: ")
            c = [{"action": "explain", "task": "Explain it for Grade 11."},
                 {"action": "worked_example", "task": "One worked example."}]
            text = json.dumps(c[n] if n < 2 else {"action": "done"})
        elif "Reply with the expression only" in p:
            text = "500*4%*3"
        else:
            text = f"Material on {TOPIC}. Ask Kila at kila.student@school.pg if unsure."
        return {"choices": [{"message": {"content": text}}], "x_nexa": {"model": model},
                "usage": {"prompt_tokens": 120, "completion_tokens": 30}}


def lesson_run(monkeypatch, approve=True, model=None):
    m = model or Model()
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", MARY, {"topic": TOPIC, "grade": "11"},
                                 auth=f"Bearer {tok(MARY)}"))
    if approve and r["status"] == "waiting_approval":
        with pytest.raises(engine.OrchestrationError):
            asyncio.run(engine.decide(r["run_id"], OTHER, "approve"))          # another school: a denial
        r = asyncio.run(engine.decide(r["run_id"], JOHN, "approve", "Checked for Grade 11."))
    return r


def recon(run_id, who=OPERATOR, **params):
    c = TestClient(runtime_app)
    return c.get(f"/v1/observability/runs/{run_id}", params=params, headers=hdr(who))


def walk(spans):
    for s in spans:
        yield s
        yield from walk(s.get("children", []))


# ---------------------------------------------------------------- reconstruction

def test_operators_reconstruct_a_multi_agent_execution(monkeypatch):
    r = lesson_run(monkeypatch)
    assert r["status"] == "succeeded"
    resp = recon(r["run_id"])
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["schema"] == "nexa.trace/1.0" and rec["view"] == "operational"
    assert rec["trace_id"] == r["trace_id"] and rec["correlation_id"] == r["run_id"]
    s = rec["summary"]
    assert s["status"] == "succeeded" and s["steps"] == 3 and s["agent_calls"] == 4
    assert s["agents"] == ["nexa-precise", "nexa-tutor"] and s["handoffs"] == 2
    assert s["model_usage"] == {"nexa-chat-precise": {"calls": 3, "prompt_tokens": 360, "completion_tokens": 90},
                                "nexa-chat-default": {"calls": 1, "prompt_tokens": 120, "completion_tokens": 30}}
    assert s["cost"].startswith("not configured") and s["policy"]["deny"] >= 1 and s["errors"] == []
    steps = rec["run"]["children"]
    assert [x["step"] for x in steps] == ["prepare", "review", "release"]
    prepare = steps[0]
    calls = prepare["children"]
    assert [(c["role"], c["agent"]) for c in calls] == [("coordinator", "nexa-precise"), ("delegate", "nexa-tutor"),
                                                         ("coordinator", "nexa-precise"), ("delegate", "nexa-precise")]
    for c in calls:
        assert c["status"] == "ok" and c["latency_ms"] is not None and c["manifest"].startswith("sha256:")
        assert c["model"]["prompt_tokens"] == 120 and c["agent_version"] == "1.0.0"
        assert c["prompt"]["digest"].startswith("sha256:") and c["output"]["chars"] > 0
    # Handoffs: coordinator result -> orchestrator decision -> delegate request, all present.
    span_ids = {c["span_id"] for c in calls}
    for hnd in rec["handoffs"]:
        assert hnd["from"] == "nexa-precise" and hnd["accepted"] and hnd["to_request"] in span_ids
    assert [hnd["to"] for hnd in rec["handoffs"]] == ["nexa-tutor", "nexa-precise"]
    # The human decision and the policy decisions are in place, in the review step.
    review = steps[1]["events"]
    assert any(e["kind"] == "checkpoint.decided" and e["detail"]["by"].startswith("user#") for e in review)
    assert any(e["kind"] == "policy.decide" and e["detail"]["rule"] == "tenancy" for e in review)
    assert any(e["kind"] == "policy.delegate" for e in prepare["events"])


def test_the_operational_view_exposes_no_secrets_or_learner_data(monkeypatch):
    r = lesson_run(monkeypatch)
    raw = json.dumps(recon(r["run_id"]).json())
    for leak in (TOPIC, "kila.student@school.pg", "user:teacher-mary", "user:teacher-john", "Checked for Grade 11",
                 rt_config.GATEWAY_TOKEN, "Bearer "):
        assert leak not in raw, leak
    assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.", raw)
    # Still useful: who did what, as stable pseudonyms; agents, models and decisions in clear.
    rec = json.loads(raw)
    assert rec["summary"]["initiator"].startswith("user#") and "nexa-precise" in raw and "tenancy" in raw
    again = recon(r["run_id"]).json()
    assert again["summary"]["initiator"] == rec["summary"]["initiator"]          # stable within the deployment


def test_forensic_view_is_for_auditors_with_a_reason_and_is_logged(monkeypatch):
    from v1.agent_runtime.state import store as state
    r = lesson_run(monkeypatch)
    assert recon(r["run_id"], STUDENT).status_code == 403
    assert recon(r["run_id"], MARY).status_code == 403                             # not an operator
    assert recon(r["run_id"], OPERATOR, view="forensic", reason="looking around").status_code == 403
    assert recon(r["run_id"], AUDITOR, view="forensic").status_code == 422          # no reason
    resp = recon(r["run_id"], AUDITOR, view="forensic", reason="Complaint 2026-11-20 about lesson content")
    assert resp.status_code == 200
    raw = json.dumps(resp.json())
    assert TOPIC in raw and "user:teacher-mary" in raw and rt_config.GATEWAY_TOKEN not in raw
    log = state.access_log(target=r["run_id"])
    assert ("forensic", "allow", "user:auditor-1") in [(a["action"], a["decision"], a["actor"]) for a in log]
    assert any(a["action"] == "forensic" and a["decision"] == "deny" for a in log)


def test_tool_calls_are_joined_with_the_tool_gateway_record(monkeypatch):
    m = Model()
    monkeypatch.setattr(gateway_client, "chat", m.chat)
    a = asyncio.run(engine.start("nexa-wf-curriculum-tutor", STUDENT, {"question": QUESTION},
                                 auth=f"Bearer {tok(STUDENT)}"))
    b = asyncio.run(engine.start("nexa-wf-curriculum-tutor", STUDENT,
                                 {"question": "Calculate the simple interest on K500 at 4% per year for 3 years."},
                                 auth=f"Bearer {tok(STUDENT)}"))
    ra = recon(a["run_id"]).json()
    tool = next(s for s in walk(ra["run"]["children"]) if s.get("kind") == "tool_call")
    assert tool["tool"] == "knowledge_answer" and tool["for_agent"] == "nexa-curriculum-tutor"
    assert tool["scopes"] == ["curriculum.learner"] and tool["call_id"].startswith("tc_")
    assert tool["gateway"]["decision"] == "allow" and tool["gateway"]["status"] == 200
    assert tool["gateway"]["connector"] == "nexa-knowledge" and tool["gateway"]["kind"] == "http"
    assert tool["retrieval"]["status"] == "grounded" and tool["retrieval"]["references"] == [
        {"marker": "S1", "title": "Biology Grade 11 Student Guide", "page": "37", "chunk_id": "chunk-bio11-042",
         "used": True}]
    assert ra["summary"]["retrieval_references"] == 1 and ra["summary"]["tool_gateway_audit"] == "joined"
    assert QUESTION not in json.dumps(ra) and "chlorophyll" not in json.dumps(ra)
    rb = recon(b["run_id"]).json()
    spans = list(walk(rb["run"]["children"]))
    assert [s["tool"] for s in spans if s.get("kind") == "tool_call"] == ["calculator"]
    assert [s["role"] for s in spans if s.get("kind") == "agent_call"] == ["agent", "agent"]
    assert rb["summary"]["tools"] == ["calculator"]


def test_errors_retries_and_denials_are_reconstructed(monkeypatch):
    r = lesson_run(monkeypatch, model=Model(fail_first=2))     # the runtime retries once, then the engine retries
    rec = recon(r["run_id"]).json()
    s = rec["summary"]
    assert s["retries"] == 1 and s["errors"][0]["code"] == "unavailable" and s["errors"][0]["retryable"] is True
    first = rec["run"]["children"][0]["children"][0]
    assert first["status"] == "error" and first["error"]["status"] == 502
    assert any(e["kind"] == "step.attempt_failed" for e in rec["run"]["children"][0]["events"])
    registry.REGISTRY.set_enabled("nexa-precise", False, "user:admin", "drill")
    r2 = lesson_run(monkeypatch, approve=False)
    s2 = recon(r2["run_id"]).json()["summary"]
    assert s2["status"] == "failed" and s2["errors"][0]["code"] == "refused"


def test_an_interrupted_and_resumed_run_shows_both_parts(monkeypatch):
    m = Model()
    gate = {"block": True}

    async def chat(*a, **k):
        if gate["block"] and m.calls == 2:
            await asyncio.Event().wait()
        return await m.chat(*a, **k)
    monkeypatch.setattr(gateway_client, "chat", chat)

    async def crash():
        r = await engine.start("nexa-wf-lesson-explainer", MARY, {"topic": TOPIC, "grade": "11"}, wait=False)
        for _ in range(200):
            if m.calls >= 2:
                break
            await asyncio.sleep(0.01)
        for t in list(engine._TASKS):
            t.cancel()
        await asyncio.sleep(0.05)
        return r["run_id"]
    rid = asyncio.run(crash())
    engine.mark_interrupted()
    gate["block"] = False
    asyncio.run(engine.resume(rid, MARY))
    rec = recon(rid).json()
    preps = [s for s in rec["run"]["children"] if s["step"] == "prepare"]
    assert [p["status"] for p in preps] == ["interrupted", "ok"]
    assert rec["summary"]["answered_from_idempotency_record"] >= 1
    assert {e["kind"] for e in rec["run"]["events"]} >= {"run.started"}
    assert any(e["kind"] == "run.resumed" for p in preps for e in p["events"]) or \
        any(e["kind"] == "run.resumed" for e in rec["run"]["events"])


# ---------------------------------------------------------------- metrics and span logs

def test_metrics_and_span_logs_carry_no_content_or_identity(monkeypatch, capsys):
    lesson_run(monkeypatch)
    text = TestClient(runtime_app).get("/metrics").text
    assert 'nexa_agent_calls_total{agent="nexa-precise",role="coordinator",status="ok"} 2' in text
    assert 'nexa_model_tokens_total{kind="prompt",model="nexa-chat-precise"} 360' in text
    assert 'nexa_workflow_runs_total{status="succeeded",workflow="nexa-wf-lesson-explainer"} 1' in text
    assert 'nexa_policy_decisions_total{action="decide",decision="deny",rule="tenancy"} 1' in text
    for leak in ("teacher-mary", TOPIC, "wfr_"):
        assert leak not in text
    spans = [json.loads(x) for x in capsys.readouterr().out.splitlines() if '"event": "span"' in x]
    agent_spans = [x for x in spans if x.get("span") == "agent.call"]
    assert len(agent_spans) == 4 and all(x["trace_id"] and x["run_id"] and x["span_id"].startswith("msg_")
                                         for x in agent_spans)
    assert all(TOPIC not in json.dumps(x) for x in spans)


def test_trace_lookup_and_text_view(monkeypatch, capsys):
    from v1.api_gateway import main as api
    from v1.tools import trace_view
    r = lesson_run(monkeypatch)
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=runtime_app))
    gw = TestClient(api.app)
    t = gw.get(f"/v1/observability/traces/{r['trace_id']}", headers=hdr(OPERATOR)).json()
    assert [x["run_id"] for x in t["runs"]] == [r["run_id"]]
    monkeypatch.setattr(trace_view.httpx, "get", lambda url, **kw: gw.get(
        url.replace("http://127.0.0.1:8099", ""), **{k: v for k, v in kw.items() if k != "timeout"}))
    assert trace_view.main([r["run_id"], "--operator", "user:ops-yasaru"]) == 0
    out = capsys.readouterr().out
    for line in ("step prepare (delegate)", "agent coordinator nexa-precise", "agent delegate    nexa-tutor",
                 "coordinator proposed 'explain': accepted", "policy decide deny rule=tenancy", "checkpoint.decided",
                 "handoffs (coordinator result → orchestrator decision → delegate request):"):
        assert line in out, line
    shown = [x for x in out.splitlines() if not x.startswith("{")]     # the CLI's output, not service log lines
    assert not [x for x in shown if TOPIC in x or "teacher-mary" in x]
    # The services' own log lines carry no content either (audit events keep the
    # acting account for accountability; the operator view pseudonymises it).
    assert not [x for x in out.splitlines() if x.startswith("{") and TOPIC in x]


def test_without_the_tool_gateway_audit_the_record_says_so(monkeypatch):
    def down(request):
        raise httpx.ConnectError("down")
    r = lesson_run(monkeypatch)
    monkeypatch.setattr(tracing_api, "TRANSPORT", httpx.MockTransport(down))
    s = recon(r["run_id"]).json()["summary"]
    assert s["tool_gateway_audit"] == "unavailable: ConnectError"


def test_old_result_payloads_still_validate_and_interrupted_runs_are_purged(monkeypatch):
    m = mc.make("result", "agent.result", correlation_id="run_x00001", trace_id="trace_0001",
                sender=mc.Party(kind="agent", id="nexa-tutor"), receiver=mc.Party(kind="orchestrator", id="nexa-orchestrator"),
                in_reply_to="msg_000000000000", payload={"output": "x", "latency_ms": 1},
                payload_schema="nexa.payload.agent.result/1")
    assert m.payload_schema == "nexa.payload.agent.result/1"
    with pytest.raises(Exception):
        mc.make("result", "agent.result", correlation_id="run_x00001", trace_id="trace_0001",
                sender=mc.Party(kind="agent", id="nexa-tutor"), receiver=mc.Party(kind="orchestrator", id="nexa-orchestrator"),
                in_reply_to="msg_000000000000", payload={"output": "x", "latency_ms": 1},
                payload_schema="nexa.payload.agent.result/9")
    monkeypatch.setattr(gateway_client, "chat", Model().chat)
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", MARY, {"topic": TOPIC, "grade": "11"}))
    registry.REGISTRY.store.query("UPDATE workflow_runs SET status = 'interrupted' WHERE run_id = ?", (r["run_id"],))
    assert purge.purge(now=time.time() + 31 * 86400)["runs_purged"] == 1
    assert bus.conversation(r["run_id"])            # the audit record stays


def test_live_check_passes_against_the_real_apps(monkeypatch, capsys):
    from v1.api_gateway import main as api
    from v1.tools import observability_check
    monkeypatch.setattr(gateway_client, "chat", Model().chat)
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=runtime_app))
    monkeypatch.setenv("NEXA_AGENT_GATEWAY_TOKEN", rt_config.GATEWAY_TOKEN)
    real_sleep = time.sleep
    with TestClient(api.app) as gw:
        monkeypatch.setattr(observability_check.httpx, "Client", lambda base_url, timeout: gw)
        monkeypatch.setattr(observability_check.time, "sleep", lambda s: real_sleep(0.02))
        code = observability_check.main([])
    out = capsys.readouterr().out
    shown = "\n".join(x for x in out.splitlines() if not x.startswith("{"))
    assert code == 0, shown
