"""NEXA-M3-001: the agent contract (nexa.agent/1.0), its validation, lifecycle
and the runtime's enforcement of it."""
import asyncio
import copy
import json
import os
from dataclasses import replace

import pytest

from v1.agent_runtime import gateway_client, manifest as mf, registry, runtime, tools
from v1.identity.principal import Principal

AGENTS = mf.AGENTS_DIR
SCHEMA = os.path.join(os.path.dirname(mf.__file__), "schema", "nexa-agent-manifest-1.0.schema.json")


def load(name):
    with open(os.path.join(AGENTS, f"{name}.json")) as fh:
        return json.load(fh)


def problems_of(raw, others=None):
    try:
        m = mf.AgentManifest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        return ["structure: " + str(exc)]
    return mf.validate_platform(m, others=others)


# ---------------------------------------------------------------- the shipped manifests

def test_every_shipped_manifest_is_valid_and_loaded():
    found = mf.load_dir(AGENTS)
    assert {"nexa-tutor", "nexa-precise", "nexa-curriculum-tutor", "nexa-lesson-planner"} <= set(found)
    assert set(registry.AGENTS) == set(found)
    states = {k: v.lifecycle.state.value for k, v in found.items()}
    assert states["nexa-tutor"] == states["nexa-precise"] == "active"
    assert states["nexa-curriculum-tutor"] == "review" and states["nexa-lesson-planner"] == "draft"


def test_published_json_schema_matches_the_contract():
    """The schema file other teams validate against is the contract itself."""
    with open(SCHEMA) as fh:
        assert json.load(fh) == mf.json_schema()
    s = mf.json_schema()
    assert s["title"] == "NEXA agent manifest (nexa.agent/1.0)"
    for section in ("identity", "purpose", "capabilities", "interface", "permissions", "tools", "model",
                    "memory", "safety", "runtime", "health", "lifecycle", "prompt"):
        assert section in s["properties"]


def test_fingerprint_is_stable_and_changes_with_the_manifest():
    a = mf.AgentManifest.model_validate(load("nexa-tutor"))
    raw = load("nexa-tutor")
    raw["prompt"]["system"] += " Be brief."
    b = mf.AgentManifest.model_validate(raw)
    assert a.fingerprint() == mf.AgentManifest.model_validate(load("nexa-tutor")).fingerprint()
    assert a.fingerprint() != b.fingerprint()


# ---------------------------------------------------------------- structure

@pytest.mark.parametrize("change,expect", [
    (lambda r: r["identity"].update(id="Tutor"), "kebab"),
    (lambda r: r["identity"].update(version="1.0"), "semantic"),
    (lambda r: r.update(colour="blue"), "Extra inputs"),
    (lambda r: r.update(manifest_version="nexa.agent/2.0"), "nexa.agent/1.0"),
    (lambda r: r["lifecycle"].update(approved_by=None), "approved_by"),
    (lambda r: r["lifecycle"].update(state="deprecated"), "sunset_on"),
    (lambda r: r.update(capabilities=["curriculum_answer"]), "citations"),
    (lambda r: r["memory"].update(long_term="read", long_term_scopes=["learner_profile"]), "memory_service"),
    (lambda r: r["lifecycle"]["changelog"][0].update(version="0.9.0"), "changelog"),
    (lambda r: r.update(capabilities=["mind_reading"]), "capabilities"),
    (lambda r: r["runtime"].update(timeout_s=0), "timeout_s"),
])
def test_structural_rules(change, expect):
    raw = copy.deepcopy(load("nexa-tutor"))
    change(raw)
    p = problems_of(raw)
    assert p and expect in " ".join(p)


# ---------------------------------------------------------------- platform rules

@pytest.mark.parametrize("change,expect", [
    (lambda r: r["model"].update(model="gpt-5-direct"), "not a logical model"),
    (lambda r: r["permissions"].update(allowed_roles=["wizard"]), "not a NEXA role"),
    (lambda r: r["permissions"].update(allowed_roles=["auditor"]), "could never run"),
    (lambda r: r["permissions"].update(run_permission="agents.everything"), "not a NEXA permission"),
    (lambda r: (r.update(tools=[{"name": "teleport", "purpose": "move a learner", "permission": "model.invoke"}]),
                r["health"].update(requires=["model_gateway", "tool_registry"])), "not registered"),
    (lambda r: r["safety"].update(policy="nexa.safety.anything/9"), "not a NEXA safety policy"),
    (lambda r: r["safety"].update(policy="nexa.safety.teacher/1"), "nexa.safety.student/1"),
    (lambda r: r["safety"].update(serves_minors=False), "serves_minors"),
    (lambda r: r["permissions"]["knowledge"].update(access_classes=["teacher_only"]), "learner content"),
    (lambda r: r["lifecycle"].update(replaced_by="nexa-ghost"), "not a known agent"),
])
def test_platform_rules(change, expect):
    raw = copy.deepcopy(load("nexa-tutor"))
    change(raw)
    p = problems_of(raw, others={"nexa-tutor": None})
    assert p and expect in " ".join(p), p


def test_an_active_agent_cannot_be_granted_a_tool_that_is_not_built_yet(monkeypatch):
    raw = copy.deepcopy(load("nexa-curriculum-tutor"))
    raw["lifecycle"].update(state="active", approved_by="A reviewer", approved_on="2026-11-20")
    assert problems_of(raw) == []                                  # the Tool Gateway serves its tools (M3-004)
    monkeypatch.setitem(tools.TOOLS, "knowledge_answer", replace(tools.TOOLS["knowledge_answer"], available=False))
    raw["lifecycle"]["state"] = "review"
    assert problems_of(raw) == []                                  # in review: allowed, it waits for the tool
    raw["lifecycle"]["state"] = "active"
    assert "not available yet" in " ".join(problems_of(raw))


def test_tool_permission_must_match_the_tool():
    raw = copy.deepcopy(load("nexa-lesson-planner"))
    raw["tools"][0]["permission"] = "model.invoke"
    assert "requires 'knowledge.query'" in " ".join(problems_of(raw))


def test_a_bad_manifest_stops_the_load(tmp_path):
    raw = copy.deepcopy(load("nexa-tutor"))
    raw["model"]["model"] = "no-such-model"
    (tmp_path / "nexa-tutor.json").write_text(json.dumps(raw))
    with pytest.raises(mf.ManifestError) as e:
        mf.load_dir(str(tmp_path))
    assert "no-such-model" in str(e.value)
    (tmp_path / "nexa-tutor.json").write_text(json.dumps(load("nexa-tutor")))
    (tmp_path / "wrong-name.json").write_text(json.dumps(load("nexa-precise")))
    with pytest.raises(mf.ManifestError, match="must be named nexa-precise.json"):
        mf.load_dir(str(tmp_path))


def test_validate_cli_reports_each_manifest(capsys, tmp_path):
    bad = copy.deepcopy(load("nexa-tutor"))
    bad["identity"]["version"] = "one"
    p = tmp_path / "nexa-tutor.json"
    p.write_text(json.dumps(bad))
    assert mf.main(["validate", os.path.join(AGENTS, "nexa-precise.json"), str(p)]) == 1
    out = capsys.readouterr().out
    assert "valid   " in out and "INVALID" in out and "semantic" in out
    assert mf.main(["validate"]) == 0


# ---------------------------------------------------------------- lifecycle

def test_lifecycle_transitions():
    S = mf.LifecycleState
    assert mf.can_transition(S.draft, S.review) and mf.can_transition(S.review, S.approved)
    assert mf.can_transition(S.approved, S.active) and mf.can_transition(S.active, S.suspended)
    assert mf.can_transition(S.active, S.deprecated) and mf.can_transition(S.deprecated, S.retired)
    assert not mf.can_transition(S.draft, S.active)          # never straight to active
    assert not mf.can_transition(S.review, S.active)
    assert not mf.can_transition(S.retired, S.active)        # retired is final
    assert mf.RUNNABLE == {S.active, S.deprecated}


# ---------------------------------------------------------------- runtime enforcement

def run(name, roles=("student",)):
    return asyncio.run(runtime.run_agent(name, Principal("user:t", tuple(roles)),
                                         [{"role": "user", "content": "hello"}]))


@pytest.fixture
def ok_gateway(monkeypatch):
    calls = []

    async def chat(model, messages, temperature=None, max_tokens=None, trace_id=None):
        calls.append((model, temperature, max_tokens))
        return {"choices": [{"message": {"content": "ok"}}], "x_nexa": {}}
    monkeypatch.setattr(gateway_client, "chat", chat)
    return calls


def test_only_runnable_agents_run(ok_gateway, monkeypatch):
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-curriculum-tutor")
    assert e.value.status_code == 409 and "review" in e.value.detail
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-lesson-planner", ("teacher",))
    assert e.value.status_code == 409
    monkeypatch.setitem(registry.AGENTS, "nexa-tutor", replace(registry.AGENTS["nexa-tutor"], state="suspended"))
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-tutor")
    assert e.value.status_code == 423


def test_allowed_roles_are_enforced(ok_gateway, monkeypatch):
    planner = replace(registry.AGENTS["nexa-lesson-planner"], state="active")
    monkeypatch.setitem(registry.AGENTS, "nexa-lesson-planner", planner)
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-lesson-planner", ("student",))                     # students hold model.invoke, but not the role
    assert e.value.status_code == 403
    assert run("nexa-lesson-planner", ("teacher",)).output == "ok"


def test_manifest_model_settings_are_applied(ok_gateway):
    r = run("nexa-precise")
    assert ok_gateway[-1] == ("nexa-chat-precise", 0.1, 600)
    assert r.x_nexa["manifest"].startswith("sha256:") and r.x_nexa["attempts"] == 1


def test_retry_then_fallback_and_no_retry_on_a_real_error(monkeypatch):
    seen = []

    async def flaky(model, messages, temperature=None, max_tokens=None, trace_id=None):
        seen.append(model)
        if model == "nexa-chat-default":
            raise gateway_client.GatewayError("gateway 503", "unavailable")
        return {"choices": [{"message": {"content": "from fallback"}}]}
    monkeypatch.setattr(gateway_client, "chat", flaky)
    spec = replace(registry.AGENTS["nexa-tutor"], fallbacks=("nexa-chat-precise",), max_attempts=2,
                   backoff_s=0.0, retry_on=("gateway_unavailable",))
    monkeypatch.setitem(registry.AGENTS, "nexa-tutor", spec)
    r = run("nexa-tutor")
    assert seen == ["nexa-chat-default", "nexa-chat-default", "nexa-chat-precise"]
    assert r.output == "from fallback" and r.x_nexa["attempts"] == 3 and r.x_nexa["fallback_model"] == "nexa-chat-precise"

    seen.clear()

    async def broken(model, messages, temperature=None, max_tokens=None, trace_id=None):
        seen.append(model)
        raise gateway_client.GatewayError("gateway 400: bad request", "error")
    monkeypatch.setattr(gateway_client, "chat", broken)
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-tutor")
    assert e.value.status_code == 502 and seen == ["nexa-chat-default"]


def test_whole_run_timeout(monkeypatch):
    async def slow(model, messages, temperature=None, max_tokens=None, trace_id=None):
        await asyncio.sleep(5)
    monkeypatch.setattr(gateway_client, "chat", slow)
    monkeypatch.setitem(registry.AGENTS, "nexa-tutor",
                        replace(registry.AGENTS["nexa-tutor"], timeout_s=0.2, max_attempts=1))
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-tutor")
    assert e.value.status_code == 504


def test_declared_tool_that_is_not_active_answers_503(monkeypatch):
    planner = replace(registry.AGENTS["nexa-lesson-planner"], state="active")
    monkeypatch.setitem(registry.AGENTS, "nexa-lesson-planner", planner)
    monkeypatch.setitem(tools.TOOLS, "course_outline", replace(tools.TOOLS["course_outline"], available=False))
    with pytest.raises(tools.ToolError) as e:
        asyncio.run(tools.invoke_tool("nexa-lesson-planner", "course_outline", Principal("user:t", ("teacher",)),
                                      user_token="Bearer t"))
    assert e.value.status_code == 503


def test_manifest_endpoint_returns_the_reviewed_manifest(monkeypatch):
    from fastapi.testclient import TestClient

    from v1.agent_runtime.main import app
    from v1.identity import tokens
    tok = tokens.issue_token("user:t", ["teacher"], "user", 60)
    with TestClient(app) as c:
        r = c.get("/v1/agents/nexa-lesson-planner/manifest", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        body = r.json()
        assert body["manifest"]["identity"]["id"] == "nexa-lesson-planner"
        assert body["fingerprint"] == registry.MANIFESTS["nexa-lesson-planner"].fingerprint()
        assert c.get("/v1/agents/nope/manifest", headers={"Authorization": f"Bearer {tok}"}).status_code == 404
        h = c.get("/health").json()
        assert h["agent_states"]["nexa-lesson-planner"] == "draft" and h["manifest_version"] == "nexa.agent/1.0"
