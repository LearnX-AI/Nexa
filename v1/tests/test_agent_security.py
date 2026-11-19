"""NEXA-M3-007 security tests: unauthorized agent actions are blocked by policy.

Each test attempts something an agent (or someone acting through one) must
not be able to do, and checks that it is refused, by which rule, and that the
refusal is recorded. Run in CI as its own step."""
import asyncio
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import config as rt_config, gateway_client, presence, registry, runtime, tools
from v1.agent_runtime.main import app as runtime_app
from v1.agent_runtime.orchestration import engine
from v1.agent_runtime.policy import contract as pc, pdp
from v1.identity import tokens
from v1.identity.principal import Principal
from v1.knowledge import access
from v1.tool_gateway import connectors, gateway, policy as tg_policy
from v1.tool_gateway.main import app as tool_app

STUDENT = Principal("user:student-1", ("student",), "user", school_id="pomnsoe")
TEACHER_A = Principal("user:teacher-a", ("teacher",), "user", school_id="pomnsoe")
TEACHER_A2 = Principal("user:teacher-a2", ("teacher",), "user", school_id="pomnsoe")
TEACHER_B = Principal("user:teacher-b", ("teacher",), "user", school_id="kerevat")
NATIONAL = Principal("user:national-1", ("national_admin",), "user")
AUDITOR = Principal("user:auditor-1", ("auditor",), "user")


def tok(p, typ="user", ttl=600, **extra):
    if p.school_id:
        extra.setdefault("school_id", p.school_id)
    return tokens.issue_token(p.subject, list(p.roles), typ, ttl, extra=extra or None)


def hdr(p):
    return {"Authorization": f"Bearer {tok(p)}"}


class Knowledge:
    def __init__(self):
        self.tokens = []

    def __call__(self, request):
        self.tokens.append(request.headers.get("authorization", "").split(" ", 1)[-1])
        return httpx.Response(200, json={"status": "grounded", "answer": "From the curriculum [S1].",
                                         "confidence": "high", "citations": [], "evidence": {"level": "strong"}})


@pytest.fixture(autouse=True)
def world(monkeypatch):
    registry.reload(":memory:")
    reg = registry.REGISTRY
    for agent, v in (("nexa-curriculum-tutor", "0.2.0"), ("nexa-lesson-planner", "0.1.0")):
        if reg._row(agent, v)["state"] == "draft":
            reg.set_state(agent, v, "review", "user:engineer")
        reg.set_state(agent, v, "approved", "user:admin", approved_by="Security test approver")
        reg.set_state(agent, v, "active", "user:admin")
    pdp.load()
    engine.load_workflows()
    gateway.load(audit_path=":memory:")
    tg_policy.LIMITER.reset()
    kn = Knowledge()
    monkeypatch.setattr(connectors, "TRANSPORT", httpx.MockTransport(kn))
    monkeypatch.setattr(tools, "TRANSPORT", httpx.ASGITransport(app=tool_app))
    monkeypatch.setattr(rt_config, "GATEWAY_TOKEN", tokens.issue_token("svc:agent-runtime", ["service"], "service",
                                                                       600))

    async def deps():
        return {k: {"ok": True} for k in ("model_gateway", "knowledge_service", "tool_gateway")} | \
            {"memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", deps)
    asyncio.run(presence.beat())
    yield kn
    registry.reload(":memory:")
    pdp.load()
    engine.load_workflows()
    gateway.load(audit_path=":memory:")


def denied(**q):
    return pdp.decisions(decision="deny", **q)


def model(monkeypatch, text="ok", choices=()):
    choices = list(choices)

    async def chat(m, messages, temperature=None, max_tokens=None, trace_id=None):
        p = messages[-1]["content"]
        if "Reply with only one JSON object" in p:
            c = choices.pop(0) if choices else {"action": "done"}
            return {"choices": [{"message": {"content": json.dumps(c)}}]}
        return {"choices": [{"message": {"content": text}}]}
    monkeypatch.setattr(gateway_client, "chat", chat)


def run(agent, who):
    return asyncio.run(runtime.run_agent(agent, who, [{"role": "user", "content": "hi"}]))


def invoke(agent, tool, who, args, scopes=None, run_id=None):
    return asyncio.run(tools.invoke_tool(agent, tool, who, args, user_token=tok(who), scopes=scopes, run_id=run_id))


# ---------------------------------------------------------------- run: default deny, roles

def test_an_agent_without_a_policy_can_do_nothing(monkeypatch):
    model(monkeypatch)
    monkeypatch.delitem(pdp.POLICIES, "nexa-tutor")
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-tutor", STUDENT)
    assert e.value.status_code == 403
    d = denied(agent="nexa-tutor", action="run")[0]
    assert d["rule"] == "default_deny" and d["actor"] == "user:student-1" and d["tenant"] == "pomnsoe"


def test_a_role_the_agent_does_not_serve_is_refused(monkeypatch):
    model(monkeypatch)
    with pytest.raises(runtime.AgentRuntimeError):
        run("nexa-lesson-planner", STUDENT)                   # teachers only
    assert denied(agent="nexa-lesson-planner", action="run")[0]["rule"] == "role"


# ---------------------------------------------------------------- invoke: least privilege, scopes

def test_policy_narrower_than_the_manifest_blocks_the_tool(world, monkeypatch):
    p = pdp.POLICIES["nexa-curriculum-tutor"]
    monkeypatch.setitem(pdp.POLICIES, "nexa-curriculum-tutor",
                        p.model_copy(update={"invoke": {"knowledge_answer": ["curriculum.learner"]}}))
    with pytest.raises(tools.ToolError) as e:
        invoke("nexa-curriculum-tutor", "calculator", STUDENT, {"expression": "1+1"})
    assert e.value.status_code == 403 and "policy does not let it invoke calculator" in e.value.detail
    assert gateway.AUDIT.query() == []                        # it never reached the Tool Gateway


def test_an_agent_cannot_escalate_its_scopes(world):
    with pytest.raises(tools.ToolError) as e:
        invoke("nexa-curriculum-tutor", "knowledge_answer", TEACHER_A, {"question": "Marking guide?"},
               scopes=["curriculum.teacher"])
    assert e.value.status_code == 403
    assert denied(agent="nexa-curriculum-tutor", action="invoke")[0]["rule"] == "scopes"
    assert gateway.AUDIT.query() == [] and world.tokens == []


def test_teacher_only_material_is_never_retrieved_for_a_student():
    d, _ = pdp.authorize_invoke("nexa-lesson-planner", "0.1.0", "knowledge_answer", ["curriculum.teacher"], STUDENT)
    assert not d.allow and d.rule == "sensitive"


def test_the_user_token_never_leaves_the_runtime(world):
    out = invoke("nexa-curriculum-tutor", "knowledge_answer", STUDENT, {"question": "What is osmosis?"})
    assert out["scopes"] == ["curriculum.learner"]
    sent = tokens.verify_token(world.tokens[-1])
    assert sent["typ"] == "delegated" and sent["act"] == "agent:nexa-curriculum-tutor@0.2.0"
    assert sent["scope"] == ["curriculum.learner"] and sent["nexa_aud"] == "nexa-tool-gateway"
    assert sent["school_id"] == "pomnsoe" and sent["exp"] - time.time() <= tools.DELEGATION_TTL_S + 1
    assert world.tokens[-1] != tok(STUDENT)


# ---------------------------------------------------------------- the Tool Gateway checks the delegation

def gw_call(token, agent="nexa-curriculum-tutor", version="0.2.0", tool="calculator", args=None, scopes=None,
            run=None):
    h = {"Authorization": f"Bearer {rt_config.GATEWAY_TOKEN}", "X-NEXA-On-Behalf-Of": f"Bearer {token}",
         "X-NEXA-Agent": agent, "X-NEXA-Agent-Version": version}
    if run:
        h["X-NEXA-Run"] = run
    return TestClient(tool_app).post(f"/v1/tools/{tool}/invoke", headers=h,
                                     json={"arguments": args or {"expression": "1+1"},
                                           **({"scopes": scopes} if scopes else {})})


def good(p=STUDENT, **over):
    claims = {"act": "agent:nexa-curriculum-tutor@0.2.0", "scope": ["compute"], "nexa_aud": "nexa-tool-gateway"}
    claims.update(over)
    return tok(p, "delegated", **claims)


def test_gateway_accepts_only_a_matching_delegation():
    assert gw_call(good()).status_code == 200
    cases = [
        (tok(STUDENT), 403, "delegated token is required"),                     # the user's own token
        (good(act="agent:nexa-lesson-planner@0.1.0"), 403, "was delegated to"),  # header spoofing
        (good(nexa_aud="nexa-knowledge"), 403, "not for the Tool Gateway"),
        (good(scope=["compute"], run_id="wfr_other"), 403, "another run"),
    ]
    for token, code, why in cases:
        r = gw_call(token, run="wfr_this")
        assert r.status_code == code and why in r.json()["detail"], (why, r.json())
    r = gw_call(good(), tool="knowledge_answer", args={"question": "Why?"}, scopes=["curriculum.learner"])
    assert r.status_code == 403 and "were not delegated" in r.json()["detail"]
    expired = tokens.issue_token("user:student-1", ["student"], "delegated", -5,
                                 extra={"act": "agent:nexa-curriculum-tutor@0.2.0", "scope": ["compute"],
                                        "nexa_aud": "nexa-tool-gateway"})
    assert gw_call(expired).status_code == 401
    checks = [c["check"] for c in gateway.AUDIT.query(decision="deny")] if False else \
        [c["check"] for c in gateway.AUDIT.query() if c["decision"] == "deny"]
    assert checks.count("delegation") == 5


def test_a_delegated_token_cannot_be_replayed_as_the_user():
    d = good(scope=["curriculum.learner"])
    r = TestClient(runtime_app).post("/v1/agents/nexa-tutor/run", json={"messages": [{"role": "user", "content": "x"}]},
                                     headers={"Authorization": f"Bearer {d}"})
    assert r.status_code == 401
    from v1.api_gateway import main as api
    assert TestClient(api.app).get("/v1/agents", headers={"Authorization": f"Bearer {d}"}).status_code == 401
    from v1.knowledge.auth import require_principal as kn_auth
    with pytest.raises(Exception) as e:
        asyncio.run(kn_auth(authorization=f"Bearer {good(nexa_aud='nexa-api-gateway')}"))
    assert getattr(e.value, "status_code", None) == 401


def test_knowledge_service_narrows_a_delegated_token_to_its_scopes():
    def principal(scopes):
        return Principal("user:teacher-a", ("teacher",), "delegated", attrs={"scope": scopes})
    assert access.allowed_classes(Principal("user:teacher-a", ("teacher",), "user")) >= {"teacher_only"}
    assert access.allowed_classes(principal(["curriculum.learner"])) == {"public", "learner"}
    assert access.allowed_classes(principal(["compute"])) == set()
    assert "teacher_only" in access.allowed_classes(principal(["curriculum.teacher"]))
    student = Principal("user:s", ("student",), "delegated", attrs={"scope": ["curriculum.teacher"]})
    assert "teacher_only" not in access.allowed_classes(student)       # the role still bounds it


# ---------------------------------------------------------------- delegate

def test_a_coordinator_cannot_delegate_outside_its_policy(monkeypatch):
    model(monkeypatch, choices=[{"action": "explain", "task": "Explain."}])
    p = pdp.POLICIES["nexa-precise"]
    monkeypatch.setitem(pdp.POLICIES, "nexa-precise", p.model_copy(update={"delegate": ["nexa-precise"]}))
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", TEACHER_A, {"topic": "Ratios", "grade": "11"}))
    assert r["status"] == "failed" and "no work was delegated" in r["error"]
    refused = [t["detail"] for t in engine.trace_of(r["run_id"]) if t["kind"] == "delegate.refused"]
    assert refused and refused[0]["delegate"] == "nexa-tutor" and refused[0]["rule"] == "delegate"
    assert not [t for t in engine.trace_of(r["run_id"]) if t["kind"] == "agent.call"
                and t["detail"].get("role") == "delegate"]                 # nexa-tutor was never called
    with pytest.raises(ValueError, match="does not let it delegate to nexa-tutor"):
        engine.load_workflows()                                            # nor would the workflow load


def test_an_agent_that_does_not_accept_delegation_is_refused():
    d = pdp.authorize_delegate("nexa-precise", "1.0.0", "nexa-curriculum-tutor", TEACHER_A)
    assert not d.allow and d.rule == "delegate"
    p = pdp.POLICIES["nexa-precise"].model_copy(update={"delegate": ["nexa-curriculum-tutor"]})
    problems = pc.check(p, dict(registry.MANIFESTS), tools.CONTRACTS, pdp.POLICIES)
    assert any("does not accept delegated work" in x for x in problems)


# ---------------------------------------------------------------- tenancy

def test_reviewers_of_another_school_cannot_see_or_decide(monkeypatch):
    model(monkeypatch, choices=[{"action": "explain", "task": "Explain."}])
    r = asyncio.run(engine.start("nexa-wf-lesson-explainer", TEACHER_A, {"topic": "Ratios", "grade": "11"}))
    assert r["status"] == "waiting_approval"
    run_ = engine.load_run(r["run_id"])
    assert run_.principal.school_id == "pomnsoe"
    assert not engine.may_view(run_, TEACHER_B) and engine.may_view(run_, TEACHER_A2) and engine.may_view(run_, NATIONAL)
    with pytest.raises(engine.OrchestrationError) as e:
        asyncio.run(engine.decide(r["run_id"], TEACHER_B, "approve"))
    assert e.value.status_code == 403 and "another school" in e.value.detail
    assert denied(action="decide")[0]["rule"] == "tenancy"
    assert asyncio.run(engine.decide(r["run_id"], TEACHER_A2, "approve"))["status"] == "succeeded"
    q = [x["run_id"] for x in engine.list_runs(TEACHER_B, "waiting_approval")]
    assert r["run_id"] not in q


# ---------------------------------------------------------------- sensitive data

def test_personal_data_is_removed_from_agents_that_serve_minors(monkeypatch):
    model(monkeypatch, text="Email kila@school.pg or call 7123 4567 for help.")
    out = run("nexa-tutor", STUDENT)
    assert out.output == "Email [email removed] or call [phone removed] for help."
    d = pdp.decisions(agent="nexa-tutor", action="redact")[0]
    assert d["rule"] == "sensitive" and d["detail"] == {"items": 2}
    assert run("nexa-lesson-planner", TEACHER_A).output.startswith("Email kila@school.pg")   # teachers' agent


# ---------------------------------------------------------------- the policies themselves

def test_a_policy_can_never_exceed_its_manifest():
    m = dict(registry.MANIFESTS)
    base = pdp.POLICIES["nexa-tutor"]
    cases = {
        "the manifest does not grant this tool": {"invoke": {"calculator": ["compute"]}},
        "not in the manifest's knowledge access classes": {"read": pc.Read(knowledge_classes=["teacher_only"])},
        "must redact personal data": {"sensitive": pc.Sensitive(redact_personal_data=False)},
        "is not a registered agent": {"delegate": ["nexa-ghost"]},
    }
    for why, update in cases.items():
        assert any(why in x for x in pc.check(base.model_copy(update=update), m, tools.CONTRACTS, pdp.POLICIES)), why


def test_every_agent_has_a_policy_and_the_schema_is_published():
    assert set(pdp.POLICIES) == set(registry.MANIFESTS)
    path = os.path.join(os.path.dirname(pc.__file__), "..", "schema", "nexa-agent-policy-1.0.schema.json")
    with open(path) as fh:
        assert json.load(fh) == pc.json_schema()


# ---------------------------------------------------------------- the decision log

def test_every_decision_is_audited_and_readable_by_auditors(monkeypatch):
    model(monkeypatch)
    run("nexa-tutor", STUDENT)
    with pytest.raises(runtime.AgentRuntimeError):
        run("nexa-lesson-planner", STUDENT)
    c = TestClient(runtime_app)
    assert c.get("/v1/policy/decisions", headers=hdr(STUDENT)).status_code == 403
    rows = c.get("/v1/policy/decisions?action=run", headers=hdr(AUDITOR)).json()["decisions"]
    assert {(r["agent"], r["decision"], r["rule"]) for r in rows} >= {("nexa-tutor", "allow", "allowed"),
                                                                      ("nexa-lesson-planner", "deny", "role")}
    assert all(r["decision_id"].startswith("pd_") and r["tenant"] == "pomnsoe" for r in rows)
    pol = c.get("/v1/policy/agents/nexa-curriculum-tutor", headers=hdr(STUDENT)).json()
    assert pol["identity"] == "agent:nexa-curriculum-tutor" and pol["fingerprint"].startswith("sha256:")


def test_live_security_check_passes_against_the_real_apps(monkeypatch, capsys):
    """v1/tools/security_check.py against the API Gateway, Agent Runtime, Tool
    Gateway and Knowledge Service apps in process (only the model is scripted)."""
    from v1.api_gateway import main as api
    from v1.knowledge.main import app as knowledge_app
    from v1.tools import security_check
    model(monkeypatch, choices=[{"action": "explain", "task": "Explain."}])
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=runtime_app))
    tg, kn = TestClient(tool_app), TestClient(knowledge_app)

    def post(url, **kw):
        kw.pop("timeout", None)
        if url.startswith("http://127.0.0.1:8105"):
            return tg.post(url.replace("http://127.0.0.1:8105", ""), **kw)
        return kn.post(url.replace("http://127.0.0.1:8103", ""), **kw)
    real_sleep = time.sleep
    monkeypatch.setenv("NEXA_AGENT_GATEWAY_TOKEN", rt_config.GATEWAY_TOKEN)
    with TestClient(api.app) as gw:
        monkeypatch.setattr(security_check.httpx, "Client", lambda base_url, timeout: gw)
        monkeypatch.setattr(security_check.httpx, "post", post)
        monkeypatch.setattr(security_check.time, "sleep", lambda s: real_sleep(0.02))
        code = security_check.main([])
    out = capsys.readouterr().out
    assert code == 0, out
    print("\n".join(x for x in out.splitlines() if x.startswith(("PASS", "FAIL", "  pd_"))))


def test_demo_runs_end_to_end_against_the_real_apps(monkeypatch, capsys):
    """v1/tools/nexa_demo.py: the guided demonstration, against the API Gateway,
    Agent Runtime and Tool Gateway in process (model and Knowledge scripted)."""
    from v1.api_gateway import main as api
    from v1.tools import nexa_demo

    async def chat(m, messages, temperature=None, max_tokens=None, trace_id=None):
        p = messages[-1]["content"]
        if "Reply with only one JSON object" in p:
            n = p.count("\nTask: ")
            c = [{"action": "explain", "task": "Explain simple interest."},
                 {"action": "worked_example", "task": "One worked example."}]
            return {"choices": [{"message": {"content": json.dumps(c[n] if n < 2 else {"action": "done"})}}]}
        if "Reply with the expression only" in p:
            return {"choices": [{"message": {"content": "500*4%*3"}}]}
        return {"choices": [{"message": {"content": "Worked answer: the interest is K60."}}]}
    monkeypatch.setattr(gateway_client, "chat", chat)
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=runtime_app))
    tg = TestClient(tool_app)
    real_sleep = time.sleep
    with TestClient(api.app) as gw:
        monkeypatch.setattr(nexa_demo.httpx, "Client", lambda base_url, timeout: gw)
        monkeypatch.setattr(nexa_demo.httpx, "get", lambda url, **kw: tg.get(
            url.replace("http://127.0.0.1:8105", ""), **{k: v for k, v in kw.items() if k != "timeout"}))
        monkeypatch.setattr(nexa_demo.time, "sleep", lambda s: real_sleep(0.02))
        code = nexa_demo.main(["--no-pause"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "demonstrated as expected" in out and "✘" not in out
