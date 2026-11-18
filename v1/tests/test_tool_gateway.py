"""NEXA-M3-004: the Tool & Connector Gateway. Tools are invoked through the
gateway with authorization and complete audit traces; allowlists, scopes,
credential isolation, timeouts, rate limits and policy checks are enforced;
MCP servers are reached through approved connectors."""
import asyncio
import json
import os
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import config as rt_config, gateway_client, presence, registry, tools
from v1.agent_runtime.main import app as runtime_app
from v1.agent_runtime.orchestration import engine
from v1.identity import tokens
from v1.identity.principal import Principal
from v1.tool_gateway import connectors, contract as ct, gateway, policy
from v1.tool_gateway.main import app as tool_app

STUDENT = Principal("user:student-1", ("student",), "user")
TEACHER = Principal("user:teacher-1", ("teacher",), "user")
SECRET = "svc-secret-0123456789abcdef"


def tok(p):
    return tokens.issue_token(p.subject, list(p.roles), "user", 600)


def hdr(p):
    return {"Authorization": f"Bearer {tok(p)}"}


class Upstream:
    """The systems behind the connectors: the Knowledge Service and an MCP server."""

    def __init__(self):
        self.seen, self.slow, self.echo_secret = [], 0.0, False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.slow:
            await asyncio.sleep(self.slow)
        if request.url.path == "/v1/knowledge/answer":
            q = json.loads(request.content)["question"]
            return httpx.Response(200, json={
                "status": "grounded", "answer": f"Grounded answer to: {q} [S1]", "confidence": "high",
                "citations": [{"marker": "S1", "used": True, "provenance": {"chunk_id": "c1", "title": "Biology 11",
                                                                            "page_label": "12"}}],
                "evidence": {"level": "strong"}})
        if request.url.path == "/mcp":
            msg = json.loads(request.content)
            if "id" not in msg:
                return httpx.Response(202)
            if msg["method"] == "initialize":
                return httpx.Response(200, headers={"mcp-session-id": "s-1"}, json={
                    "jsonrpc": "2.0", "id": msg["id"],
                    "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "glossary-mcp"},
                               "capabilities": {"tools": {}}}})
            if msg["method"] == "tools/call":
                term = msg["params"]["arguments"]["term"]
                text = f"{term}: a word in the glossary"
                if self.echo_secret:
                    text += " " + request.headers.get("authorization", "")
                body = {"jsonrpc": "2.0", "id": msg["id"],
                        "result": {"content": [{"type": "text", "text": text}], "isError": False}}
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=f"event: message\ndata: {json.dumps(body)}\n\n")
        return httpx.Response(404)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Registry with nexa-curriculum-tutor 0.2.0 approved and active, the Tool
    Gateway in process, and fake upstream systems. Plus an approved MCP
    connector and tool, only in this test's contract directory."""
    d_tools, d_conn = tmp_path / "tools", tmp_path / "connectors"
    shutil.copytree(ct.TOOLS_DIR, d_tools)
    shutil.copytree(ct.CONNECTORS_DIR, d_conn)
    (d_conn / "glossary-mcp.json").write_text(json.dumps({
        "tool_version": "nexa.tool/1.0", "id": "glossary-mcp", "name": "Glossary MCP server", "kind": "mcp",
        "owner": "Test", "url_env": "NEXA_TEST_GLOSSARY_URL", "default_url": "http://glossary.test/mcp",
        "credential": {"mode": "service", "env": "NEXA_TEST_GLOSSARY_TOKEN"}, "approved": True,
        "approved_by": "Test reviewer"}))
    (d_tools / "glossary.json").write_text(json.dumps({
        "tool_version": "nexa.tool/1.0", "id": "glossary", "name": "Glossary lookup", "version": "1.0.0",
        "owner": "Test", "description": "Look a term up in an external glossary (MCP).", "state": "active",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["term"],
                         "properties": {"term": {"type": "string", "maxLength": 200}}},
        "permission": "model.invoke", "scopes": {"lookup": {"description": "Read the glossary.", "roles": ["student"]}},
        "default_scopes": ["lookup"], "allowed_agents": {"nexa-curriculum-tutor": ["lookup"]},
        "timeout_s": 5, "data": {"returns": "public", "leaves_nexa": True},
        "implementation": {"kind": "mcp", "connector": "glossary-mcp", "mcp_tool": "define"}}))
    monkeypatch.setenv("NEXA_TEST_GLOSSARY_TOKEN", SECRET)
    gateway.load(str(d_tools), str(d_conn), ":memory:")
    policy.LIMITER.reset()
    up = Upstream()
    monkeypatch.setattr(connectors, "TRANSPORT", httpx.MockTransport(up))
    monkeypatch.setattr(tools, "TRANSPORT", httpx.ASGITransport(app=tool_app))
    monkeypatch.setattr(rt_config, "GATEWAY_TOKEN", tokens.issue_token("svc:agent-runtime", ["service"], "service", 600))
    registry.reload(":memory:")
    reg = registry.REGISTRY
    reg.set_state("nexa-curriculum-tutor", "0.2.0", "approved", "user:admin", approved_by="Test approver")
    reg.set_state("nexa-curriculum-tutor", "0.2.0", "active", "user:admin")
    engine.load_workflows()
    yield up
    gateway.load(audit_path=":memory:")
    registry.reload(":memory:")
    engine.load_workflows()


@pytest.fixture
def rt():
    return TestClient(runtime_app)


def audit(**q):
    return gateway.AUDIT.query(**q)


# ---------------------------------------------------------------- tools through the gateway

def test_knowledge_answer_through_the_gateway_as_the_user(world, rt):
    r = rt.post("/v1/agents/nexa-curriculum-tutor/tools/knowledge_answer",
                json={"arguments": {"question": "What is osmosis?"}}, headers=hdr(STUDENT) | {"X-Trace-Id": "t-ka"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["status"] == "grounded" and body["result"]["citations"][0]["title"] == "Biology 11"
    assert body["scopes"] == ["curriculum.learner"] and body["x_nexa"]["connector"] == "nexa-knowledge"
    # The Knowledge Service saw the student's own token: their access applies.
    sent = world.seen[-1]
    assert tokens.verify_token(sent.headers["authorization"].split()[1])["sub"] == "user:student-1"
    a = audit(call_id=body["call_id"])[0]
    assert a["decision"] == "allow" and a["status"] == 200 and a["user"] == "user:student-1"
    assert a["agent"] == "nexa-curriculum-tutor" and a["agent_version"] == "0.2.0"
    assert a["tool"] == "knowledge_answer" and a["tool_version"] == "1.0.0" and a["kind"] == "http"
    assert a["scopes"] == ["curriculum.learner"] and a["args_digest"].startswith("sha256:")
    assert a["output_bytes"] > 0 and a["output_digest"].startswith("sha256:") and a["latency_ms"] is not None
    assert a["trace_id"] == body["x_nexa"]["trace_id"]


def test_calculator_through_the_gateway(world, rt):
    r = rt.post("/v1/agents/nexa-curriculum-tutor/tools/calculator", json={"arguments": {"expression": "500*4%*3"}},
                headers=hdr(STUDENT))
    assert r.status_code == 200 and r.json()["result"]["exact"] == "60"
    r = rt.post("/v1/agents/nexa-curriculum-tutor/tools/calculator", json={"arguments": {"expression": "1/3+1/6"}},
                headers=hdr(STUDENT))
    assert r.json()["result"]["text"] == "1/3+1/6 = 1/2 = 0.5"
    assert [c["tool"] for c in audit(tool="calculator")] == ["calculator", "calculator"]


def test_mcp_tool_with_an_isolated_service_credential(world, rt):
    r = rt.post("/v1/agents/nexa-curriculum-tutor/tools/glossary", json={"arguments": {"term": "osmosis"}},
                headers=hdr(STUDENT))
    # The manifest does not grant glossary: the runtime refuses before the gateway.
    assert r.status_code == 403 and audit(tool="glossary") == []
    # Called for an agent that is granted it (gateway-level), the MCP path works.
    caller = gateway.Caller(Principal("svc:agent-runtime", ("service",)), STUDENT, tok(STUDENT),
                            "nexa-curriculum-tutor", "0.2.0", None, None, "t-mcp")
    out = asyncio.run(gateway.invoke("glossary", caller, {"term": "osmosis"}))
    assert out["result"]["text"] == "osmosis: a word in the glossary" and out["result"]["server"] == "glossary-mcp"
    methods = [json.loads(x.content)["method"] for x in world.seen]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert world.seen[-1].headers["authorization"] == f"Bearer {SECRET}"          # service credential used...
    assert world.seen[-1].headers["mcp-session-id"] == "s-1"
    assert SECRET not in json.dumps(out) and SECRET not in json.dumps(audit())     # ...and never seen again
    world.echo_secret = True
    with pytest.raises(gateway.GatewayError) as e:
        asyncio.run(gateway.invoke("glossary", caller, {"term": "x"}))
    assert e.value.status_code == 403 and "credential" in e.value.detail
    with pytest.raises(gateway.GatewayError) as e:                                  # no personal data leaves NEXA
        asyncio.run(gateway.invoke("glossary", caller, {"term": "mail me at kila@school.pg"}))
    assert e.value.status_code == 422 and audit()[0]["check"] == "data"
    assert "kila@school.pg" not in json.dumps(audit())


# ---------------------------------------------------------------- authorization and policy, all audited

def test_only_the_runtime_may_call_the_gateway(world):
    gw = TestClient(tool_app)
    r = gw.post("/v1/tools/calculator/invoke", json={"arguments": {"expression": "1+1"}},
                headers=hdr(STUDENT) | {"X-NEXA-On-Behalf-Of": f"Bearer {tok(STUDENT)}",
                                        "X-NEXA-Agent": "nexa-curriculum-tutor"})
    assert r.status_code == 403 and audit()[0]["check"] == "caller"
    assert gw.post("/v1/tools/calculator/invoke", json={}, headers=hdr(STUDENT)).status_code == 400
    assert gw.post("/v1/tools/calculator/invoke", json={}).status_code == 401


def svc_call(agent, user, tool, args, scopes=None):
    gw = TestClient(tool_app)
    return gw.post(f"/v1/tools/{tool}/invoke", json={"arguments": args, **({"scopes": scopes} if scopes else {})},
                   headers={"Authorization": f"Bearer {rt_config.GATEWAY_TOKEN}",
                            "X-NEXA-On-Behalf-Of": f"Bearer {tok(user)}", "X-NEXA-Agent": agent})


def test_allowlist_permission_scopes_and_arguments(world):
    r = svc_call("nexa-tutor", STUDENT, "calculator", {"expression": "1+1"})
    assert r.status_code == 403 and "allowlist" in r.json()["detail"]
    r = svc_call("nexa-lesson-planner", Principal("user:a", ("lms_sync",)), "knowledge_answer", {"question": "Why?"})
    assert r.status_code == 403 and audit()[0]["check"] == "permission"
    r = svc_call("nexa-lesson-planner", STUDENT, "knowledge_answer", {"question": "Marking guide?"},
                 ["curriculum.teacher"])
    assert r.status_code == 403 and "not open to this user's role" in r.json()["detail"]
    r = svc_call("nexa-curriculum-tutor", TEACHER, "knowledge_answer", {"question": "Marking guide?"},
                 ["curriculum.teacher"])
    assert r.status_code == 403 and "not granted to this agent" in r.json()["detail"]
    r = svc_call("nexa-lesson-planner", TEACHER, "knowledge_answer",
                 {"question": "Marking guide?", "audience": "teacher"}, ["curriculum.teacher"])
    assert r.status_code == 200
    r = svc_call("nexa-curriculum-tutor", STUDENT, "calculator", {"expression": "__import__('os')"})
    assert r.status_code == 422 and audit()[0]["check"] == "arguments"
    r = svc_call("nexa-curriculum-tutor", STUDENT, "calculator", {"expression": "2^1000"})
    assert r.status_code == 422 and audit()[0]["check"] == "tool"
    r = svc_call("nexa-curriculum-tutor", STUDENT, "calculator", {"expression": "1", "extra": 1})
    assert r.status_code == 422
    decisions = [(c["decision"], c["check"]) for c in audit(limit=20)]
    assert ("deny", "allowlist") in decisions and ("deny", "scopes") in decisions


def test_rate_limit_per_user(world, monkeypatch):
    t = gateway.TOOLS["calculator"]
    monkeypatch.setitem(gateway.TOOLS, "calculator", t.model_copy(update={
        "rate_limit": ct.RateLimit(per_user_per_minute=2, per_tool_per_minute=100)}))
    codes = [svc_call("nexa-curriculum-tutor", STUDENT, "calculator", {"expression": "1+1"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429] and audit()[0]["check"] == "rate"
    assert svc_call("nexa-curriculum-tutor", TEACHER, "calculator", {"expression": "1+1"}).status_code == 200


def test_tool_timeout_and_unreachable_connector(world, monkeypatch):
    world.slow = 2
    t = gateway.TOOLS["knowledge_answer"]
    monkeypatch.setitem(gateway.TOOLS, "knowledge_answer", t.model_copy(update={"timeout_s": 0.5}))
    r = svc_call("nexa-curriculum-tutor", STUDENT, "knowledge_answer", {"question": "Slow?"})
    assert r.status_code == 504 and audit()[0]["check"] == "timeout"

    def down(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(connectors, "TRANSPORT", httpx.MockTransport(down))
    r = svc_call("nexa-curriculum-tutor", STUDENT, "knowledge_answer", {"question": "Down?"})
    assert r.status_code == 503 and audit()[0]["check"] == "connector"


def test_runtime_checks_before_the_gateway(world, rt):
    r = rt.post("/v1/agents/nexa-tutor/tools/calculator", json={"arguments": {"expression": "1+1"}},
                headers=hdr(STUDENT))
    assert r.status_code == 403                                     # not granted in nexa-tutor's manifest
    r = rt.post("/v1/agents/nexa-lesson-planner/tools/calculator", json={"arguments": {"expression": "1+1"}},
                headers=hdr(TEACHER))
    assert r.status_code == 409                                     # a draft agent uses no tools
    registry.REGISTRY.set_enabled("nexa-curriculum-tutor", False, "user:admin", "drill")
    r = rt.post("/v1/agents/nexa-curriculum-tutor/tools/calculator", json={"arguments": {"expression": "1+1"}},
                headers=hdr(STUDENT))
    assert r.status_code == 423
    assert audit() == []                                            # none of these reached the gateway


def test_max_calls_per_run(world):
    async def calls():
        out = []
        for _ in range(4):
            try:
                await tools.invoke_tool("nexa-curriculum-tutor", "calculator", STUDENT, {"expression": "2+2"},
                                        user_token=tok(STUDENT), run_id="wfr_test")
                out.append(200)
            except tools.ToolError as exc:
                out.append(exc.status_code)
        return out
    assert asyncio.run(calls()) == [200, 200, 200, 429]             # max_calls_per_run 3 in the manifest


def test_audit_endpoint_is_for_auditors(world, rt):
    rt.post("/v1/agents/nexa-curriculum-tutor/tools/calculator", json={"arguments": {"expression": "6*7"}},
            headers=hdr(STUDENT))
    gw = TestClient(tool_app)
    assert gw.get("/v1/tools/audit", headers=hdr(STUDENT)).status_code == 403
    calls = gw.get("/v1/tools/audit?tool=calculator", headers=hdr(Principal("user:aud", ("auditor",)))).json()["calls"]
    assert calls[0]["args_preview"] == '{"expression": "6*7"}'
    h = gw.get("/health").json()
    assert h["tools"]["calculator"]["implementation"] == "builtin" and h["connectors"]["glossary-mcp"]["approved"]
    assert SECRET not in json.dumps(gw.get("/v1/connectors", headers=hdr(Principal("user:ad", ("admin",)))).json())


# ---------------------------------------------------------------- tools in a workflow

class Model:
    def __init__(self):
        self.prompts = []

    async def chat(self, model, messages, temperature=None, max_tokens=None, trace_id=None):
        p = messages[-1]["content"]
        self.prompts.append(p)
        if "Reply with the expression only" in p:
            return {"choices": [{"message": {"content": "500*4%*3"}}]}
        return {"choices": [{"message": {"content": "Worked answer: K60."}}]}


def test_workflow_tool_steps_with_full_trace_and_audit(world, monkeypatch):
    m = Model()
    monkeypatch.setattr(gateway_client, "chat", m.chat)

    async def deps():
        return {"model_gateway": {"ok": True}, "knowledge_service": {"ok": True}, "tool_gateway": {"ok": True},
                "memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", deps)
    r = asyncio.run(engine.start("nexa-wf-curriculum-tutor", STUDENT,
                                 {"question": "Calculate the simple interest on K500 at 4% for 3 years"},
                                 auth=f"Bearer {tok(STUDENT)}"))
    assert r["status"] == "succeeded" and r["output"] == "Worked answer: K60."
    tr = engine.trace_of(r["run_id"])
    call = next(t["detail"] for t in tr if t["kind"] == "tool.call")
    assert call["tool"] == "calculator" and call["ok"] and call["call_id"].startswith("tc_")
    assert "calculator tool computed 500*4%*3 = 60" in m.prompts[-1]
    a = audit(run_id=r["run_id"])
    assert [(x["tool"], x["step"], x["trace_id"]) for x in a] == [("calculator", "compute", r["trace_id"])]
    # The other path: a curriculum question through knowledge_answer.
    r = asyncio.run(engine.start("nexa-wf-curriculum-tutor", STUDENT, {"question": "What is osmosis?"},
                                 auth=f"Bearer {tok(STUDENT)}"))
    assert r["status"] == "succeeded" and r["output"].startswith("Grounded answer to: What is osmosis?")
    assert [x["tool"] for x in audit(run_id=r["run_id"])] == ["knowledge_answer"]


# ---------------------------------------------------------------- contracts

def raw(tool):
    with open(os.path.join(ct.TOOLS_DIR, f"{tool}.json")) as fh:
        return json.load(fh)


def test_contract_rules():
    t = raw("calculator")
    t["side_effects"] = "write"
    with pytest.raises(Exception, match="never available to students"):
        ct.ToolContract.model_validate(t)
    t = raw("knowledge_answer")
    t["data"]["returns"] = "teacher_only"
    with pytest.raises(Exception, match="no scope for students"):
        ct.ToolContract.model_validate(t)
    t = raw("calculator")
    t["allowed_agents"]["nexa-tutor"] = ["write_everything"]
    with pytest.raises(Exception, match="grant one or more"):
        ct.ToolContract.model_validate(t)
    t = raw("calculator")
    t["input_schema"]["additionalProperties"] = True
    with pytest.raises(Exception, match="additionalProperties"):
        ct.ToolContract.model_validate(t)


def test_an_active_tool_cannot_use_an_unapproved_mcp_server(tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "t").mkdir()
    (tmp_path / "c" / "x-mcp.json").write_text(json.dumps({
        "tool_version": "nexa.tool/1.0", "id": "x-mcp", "name": "Unreviewed", "kind": "mcp", "owner": "Someone",
        "url_env": "NEXA_X_URL", "credential": {"mode": "none"}}))
    t = raw("calculator")
    t.update(id="x_tool", implementation={"kind": "mcp", "connector": "x-mcp", "mcp_tool": "anything"})
    (tmp_path / "t" / "x_tool.json").write_text(json.dumps(t))
    with pytest.raises(ct.ContractError, match="not approved"):
        ct.load(str(tmp_path / "t"), str(tmp_path / "c"))


def test_shipped_contracts_and_schema():
    found, conns = ct.load()
    assert {"knowledge_answer", "course_outline", "calculator"} <= set(found) and "nexa-knowledge" in conns
    assert tools.TOOLS["knowledge_answer"].available
    with open(os.path.join(ct.ROOT, "schema", "nexa-tool-1.0.schema.json")) as fh:
        assert json.load(fh) == ct.json_schema()


def test_calculator_is_exact_and_refuses_anything_else():
    from v1.tool_gateway.handlers import HandlerError, evaluate
    assert evaluate("0.1+0.2") == evaluate("3/10")
    assert str(evaluate("(2/3)^2")) == "4/9"
    for bad in ("open('x')", "2**10**10", "1/0", "a+1"):
        with pytest.raises(HandlerError):
            evaluate(bad)
