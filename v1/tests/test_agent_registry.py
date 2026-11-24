"""NEXA-M3-002: agent registry and discovery. Agents can register, be queried
by capability, be versioned, be disabled, and every change is audited."""
import asyncio
import copy
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime import gateway_client, manifest as mf, presence, registry, runtime
from v1.agent_runtime.main import app
from v1.identity import tokens
from v1.identity.principal import Principal


@pytest.fixture(autouse=True)
def fresh_registry(tmp_path_factory):
    """Each test gets a registry seeded from the shipped current manifests
    (without the versions proposed for review, NEXA-M4-001)."""
    import shutil
    d = tmp_path_factory.mktemp("agents")
    for f in os.listdir(mf.AGENTS_DIR):
        if f.endswith(".json"):
            shutil.copy(os.path.join(mf.AGENTS_DIR, f), d / f)
    registry.reload(":memory:", directory=str(d))
    yield
    registry.reload(":memory:")


@pytest.fixture
def client():
    return TestClient(app)


def hdr(roles, sub=None):
    sub = sub or f"user:{roles[0]}"
    return {"Authorization": f"Bearer {tokens.issue_token(sub, list(roles), 'user', 300)}"}


def tutor_raw(version="1.1.0", change="Shorter answers.", state="draft"):
    with open(os.path.join(mf.AGENTS_DIR, "nexa-tutor.json")) as fh:
        raw = json.load(fh)
    raw["identity"]["version"] = version
    # A new version of an education agent runs under the teaching policy (NEXA-M4-001).
    raw["prompt"]["system"] = "You are NEXA Tutor, for secondary school students in Papua New Guinea. Keep answers short."
    raw["pedagogy"] = {"policy": "nexa-teaching-policy", "version": "1.0.0", "profile": "tutor"}
    lc = raw["lifecycle"]
    lc.update(state=state, approved_by=None, approved_on=None)
    lc["changelog"] = [{"version": version, "date": "2026-11-18", "change": change}] + lc["changelog"]
    return raw


@pytest.fixture
def ok_gateway(monkeypatch):
    async def chat(model, messages, temperature=None, max_tokens=None, trace_id=None):
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(gateway_client, "chat", chat)


def run(name, roles=("student",)):
    return asyncio.run(runtime.run_agent(name, Principal("user:t", roles, "user"),
                                         [{"role": "user", "content": "hi"}]))


def healthy(monkeypatch):
    async def deps():
        return {"model_gateway": {"ok": True}, "knowledge_service": {"ok": True},
                "memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", deps)


# ---------------------------------------------------------------- seeding

def test_seed_registers_every_manifest_once():
    reg = registry.REGISTRY
    assert set(reg.agent_ids()) == {"nexa-tutor", "nexa-precise", "nexa-curriculum-tutor", "nexa-lesson-planner"}
    assert set(registry.AGENTS) == set(reg.agent_ids())
    events = reg.history("nexa-tutor")
    assert [e["action"] for e in reversed(events)] == ["agent.created", "version.registered"]
    assert events[0]["actor"] == registry.SEED_ACTOR
    reg.seed()                       # the shipped files, and the versions proposed for review (NEXA-M4-001)
    assert {r["result"] for r in reg.seed()} == {"unchanged"}       # idempotent
    assert [e["action"] for e in reversed(reg.history("nexa-tutor"))] == [
        "agent.created", "version.registered", "version.registered"]
    assert registry.AGENTS["nexa-tutor"].version == "1.0.0"           # the proposal is in review: 1.0.0 serves


def test_registry_state_survives_a_restart_and_the_file_does_not_override_it(tmp_path):
    db = str(tmp_path / "agents.sqlite")
    reg = registry.reload(db)
    reg.set_state("nexa-tutor", "1.0.0", "suspended", "user:admin", reason="incident 42")
    reg.set_enabled("nexa-precise", False, "user:admin", "model drift")
    reg = registry.reload(db)                                         # restart: the seed runs again
    assert reg.spec("nexa-tutor", "1.0.0").state == "suspended"         # the file still says active
    assert registry.AGENTS["nexa-precise"].enabled is False
    assert reg.history("nexa-tutor")[0]["detail"]["reason"] == "incident 42"


def test_a_changed_manifest_file_without_a_new_version_is_refused(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    raw = json.load(open(os.path.join(mf.AGENTS_DIR, "nexa-tutor.json")))
    (d / "nexa-tutor.json").write_text(json.dumps(raw))
    db = str(tmp_path / "agents.sqlite")
    registry.reload(db, str(d))
    raw["prompt"]["system"] += " Changed."
    (d / "nexa-tutor.json").write_text(json.dumps(raw))
    with pytest.raises(registry.RegistryError) as e:
        registry.reload(db, str(d))
    assert e.value.status_code == 409 and "immutable" in e.value.detail


# ---------------------------------------------------------------- register and version

def test_register_a_new_version_and_versions_are_immutable(client):
    eng = hdr(["ai_engineer"], "user:engineer")
    r = client.post("/v1/registry/agents", json=tutor_raw(), headers=eng)
    assert r.status_code == 201 and r.json()["result"] == "registered" and r.json()["state"] == "draft"
    again = client.post("/v1/registry/agents", json=tutor_raw(), headers=eng)
    assert again.status_code == 201 and again.json()["result"] == "unchanged"
    changed = tutor_raw()
    changed["prompt"]["system"] += " Different."
    r = client.post("/v1/registry/agents", json=changed, headers=eng)
    assert r.status_code == 409 and "immutable" in r.json()["detail"]
    r = client.post("/v1/registry/agents", json=tutor_raw("0.9.0"), headers=eng)
    assert r.status_code == 409 and "not newer" in r.json()["detail"]
    body = client.get("/v1/registry/agents/nexa-tutor", headers=eng).json()
    assert body["versions"] == ["1.0.0", "1.1.0"]
    assert body["version"] == "1.0.0"                       # 1.1.0 is a draft: 1.0.0 still serves
    assert registry.AGENTS["nexa-tutor"].version == "1.0.0"


def test_invalid_manifests_and_permissions(client):
    eng = hdr(["ai_engineer"])
    bad = tutor_raw()
    del bad["safety"]
    r = client.post("/v1/registry/agents", json=bad, headers=eng)
    assert r.status_code == 422 and any("safety" in p for p in r.json()["detail"])
    wrong = tutor_raw()
    wrong["model"]["model"] = "gpt-unknown"
    r = client.post("/v1/registry/agents", json=wrong, headers=eng)
    assert r.status_code == 422 and "not a logical model" in r.json()["detail"][0]
    for roles in (["teacher"], ["student"], ["auditor"]):
        assert client.post("/v1/registry/agents", json=tutor_raw(), headers=hdr(roles)).status_code == 403
    assert client.get("/v1/registry/agents").status_code == 401


def test_full_lifecycle_promotes_the_new_version(client, ok_gateway):
    eng, admin = hdr(["ai_engineer"], "user:engineer"), hdr(["admin"], "user:admin")
    client.post("/v1/registry/agents", json=tutor_raw(), headers=eng)
    url = "/v1/registry/agents/nexa-tutor/versions/1.1.0/state"
    assert client.put(url, json={"state": "review"}, headers=admin).status_code == 200
    # An engineer cannot approve; nor can the identity that registered it.
    assert client.put(url, json={"state": "approved", "approved_by": "X"}, headers=eng).status_code == 403
    r = client.put(url, json={"state": "approved"}, headers=admin)
    assert r.status_code == 422 and "approved_by" in r.json()["detail"]
    r = client.put(url, json={"state": "active"}, headers=admin)
    assert r.status_code == 409 and "allowed: approved, draft" in r.json()["detail"]
    r = client.put(url, json={"state": "approved", "approved_by": "Mr. Chandana"}, headers=admin)
    assert r.status_code == 200 and r.json()["state"] == "approved"
    before = registry.REGISTRY.versions("nexa-tutor")[1]["fingerprint"]
    assert client.put(url, json={"state": "active"}, headers=admin).status_code == 200
    v = registry.REGISTRY.versions("nexa-tutor")[1]
    assert v["fingerprint"] == before                       # approval changes the state, not the version
    assert v["approved_by"] == "Mr. Chandana" and v["state_changed_by"] == "user:admin"
    assert registry.AGENTS["nexa-tutor"].version == "1.1.0"   # the request path now runs 1.1.0
    assert run("nexa-tutor").x_nexa["agent_version"] == "1.1.0"
    # The old version can still be fetched and pinned.
    assert registry.get_agent("nexa-tutor", "1.0.0").version == "1.0.0"
    assert client.get("/v1/registry/agents/nexa-tutor/versions/1.0.0", headers=eng).status_code == 200


def test_four_eyes_the_registrant_cannot_approve():
    reg = registry.REGISTRY
    reg.register(tutor_raw(), "user:admin")
    reg.set_state("nexa-tutor", "1.1.0", "review", "user:admin")
    with pytest.raises(registry.RegistryError) as e:
        reg.set_state("nexa-tutor", "1.1.0", "approved", "user:admin", approved_by="me")
    assert e.value.status_code == 403


def test_deprecation_needs_a_sunset_and_retired_is_final(client, ok_gateway):
    admin = hdr(["admin"], "user:admin")
    url = "/v1/registry/agents/nexa-precise/versions/1.0.0/state"
    assert client.put(url, json={"state": "deprecated"}, headers=admin).status_code == 422
    r = client.put(url, json={"state": "deprecated", "sunset_on": "2099-01-01", "replaced_by": "nexa-tutor"},
                   headers=admin)
    assert r.status_code == 200
    assert registry.AGENTS["nexa-precise"].replaced_by == "nexa-tutor" and run("nexa-precise").output == "ok"
    assert client.put(url, json={"state": "retired"}, headers=admin).status_code == 200
    assert client.put(url, json={"state": "active"}, headers=admin).status_code == 409
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-precise")
    assert e.value.status_code == 410


# ---------------------------------------------------------------- disable

def test_disable_is_a_kill_switch_and_is_audited(client, ok_gateway):
    admin = hdr(["admin"], "user:admin")
    assert client.post("/v1/registry/agents/nexa-tutor/disable", json={"reason": "x"},
                       headers=hdr(["teacher"])).status_code == 403
    assert client.post("/v1/registry/agents/nexa-tutor/disable", json={}, headers=admin).status_code == 422
    r = client.post("/v1/registry/agents/nexa-tutor/disable", json={"reason": "unsafe answers reported"},
                    headers=admin)
    assert r.status_code == 200 and r.json()["result"] == "changed"
    with pytest.raises(runtime.AgentRuntimeError) as e:
        run("nexa-tutor")
    assert e.value.status_code == 423 and "unsafe answers reported" in e.value.detail
    assert registry.AGENTS["nexa-tutor"].state == "active"   # the lifecycle is untouched
    assert client.get("/health").json()["registry"]["disabled"] == ["nexa-tutor"]
    assert client.post("/v1/registry/agents/nexa-tutor/enable", json={}, headers=admin).status_code == 200
    assert run("nexa-tutor").output == "ok"
    events = client.get("/v1/registry/agents/nexa-tutor/history", headers=hdr(["auditor"])).json()["events"]
    assert [e["action"] for e in events[:2]] == ["agent.enabled", "agent.disabled"]
    assert events[1]["actor"] == "user:admin" and events[1]["detail"]["reason"] == "unsafe answers reported"


# ---------------------------------------------------------------- query and discovery

def test_query_by_capability_audience_and_state(client):
    h = hdr(["teacher"])
    names = lambda r: sorted(a["agent_id"] for a in r.json()["agents"])  # noqa: E731
    assert names(client.get("/v1/registry/agents?capability=explanation", headers=h)) == \
        sorted(i for i, m in registry.MANIFESTS.items() if "explanation" in [c.value for c in m.capabilities])
    assert names(client.get("/v1/registry/agents?capability=lesson_planning", headers=h)) == ["nexa-lesson-planner"]
    assert names(client.get("/v1/registry/agents?state=review", headers=h)) == ["nexa-curriculum-tutor"]
    runnable = names(client.get("/v1/registry/agents?runnable=true", headers=h))
    assert runnable == ["nexa-precise", "nexa-tutor"]
    assert names(client.get("/v1/registry/agents?capability=translation", headers=h)) == []


def test_discover_returns_only_what_the_caller_may_run_where_it_is_healthy(client, monkeypatch):
    healthy(monkeypatch)
    monkeypatch.setenv("NEXA_AGENT_LOCATION", "core:dgx")
    monkeypatch.setenv("NEXA_AGENT_INSTANCE", "dgx-1")
    beats = asyncio.run(presence.beat())
    assert {b["deployment_id"] for b in beats} >= {"dgx-1/nexa-tutor@1.0.0"}
    student = hdr(["student"])
    r = client.get("/v1/registry/discover?capability=chat&location=core:dgx", headers=student).json()
    found = {a["agent_id"]: a for a in r["agents"]}
    assert "nexa-tutor" in found and "nexa-lesson-planner" not in found     # draft, and teachers only
    dep = found["nexa-tutor"]["deployments"][0]
    assert dep["location"] == "core:dgx" and dep["endpoint"] == "http://127.0.0.1:8102" and dep["healthy"]
    # A second deployment at the school is preferred for a caller there.
    reg = registry.REGISTRY
    d = reg.register_deployment("nexa-tutor", "1.0.0", location="school:pomnsoe", endpoint="http://10.20.50.11:8102",
                                instance="jetson-1", actor="runtime:jetson-1")["deployment_id"]
    reg.heartbeat(d, {"ready": True, "checks": {}}, "runtime:jetson-1")
    r = client.get("/v1/registry/discover?capability=chat&location=school:pomnsoe", headers=student).json()
    tutor = next(a for a in r["agents"] if a["agent_id"] == "nexa-tutor")
    assert [x["location"] for x in tutor["deployments"]] == ["school:pomnsoe", "core:dgx"]
    assert client.get("/v1/registry/resolve/nexa-tutor?location=school:pomnsoe",
                      headers=student).json()["deployment"]["instance"] == "jetson-1"
    # Stale heartbeat or unhealthy dependency: not offered.
    reg.store.query("UPDATE deployments SET last_heartbeat = ? WHERE instance = 'jetson-1'",
                    (time.time() - 3600,))
    r = client.get("/v1/registry/discover?capability=chat&location=school:pomnsoe", headers=student).json()
    tutor = next(a for a in r["agents"] if a["agent_id"] == "nexa-tutor")
    assert [x["location"] for x in tutor["deployments"]] == ["core:dgx"]
    # Disabled: not offered at all.
    reg.set_enabled("nexa-tutor", False, "user:admin", "test")
    r = client.get("/v1/registry/discover?capability=chat", headers=student).json()
    assert "nexa-tutor" not in {a["agent_id"] for a in r["agents"]}


def test_heartbeat_audits_health_changes_only(monkeypatch):
    healthy(monkeypatch)
    monkeypatch.setenv("NEXA_AGENT_INSTANCE", "dgx-1")
    asyncio.run(presence.beat())
    asyncio.run(presence.beat())
    acts = [e["action"] for e in registry.REGISTRY.history("nexa-tutor")]
    assert acts.count("deployment.registered") == 1 and "deployment.health" not in acts

    async def down():
        return {"model_gateway": {"ok": False, "error": "ConnectError"}, "knowledge_service": {"ok": True},
                "memory_service": {"ok": False}}
    monkeypatch.setattr(presence, "dependency_checks", down)
    beats = asyncio.run(presence.beat())
    assert not next(b for b in beats if b["deployment_id"] == "dgx-1/nexa-tutor@1.0.0")["ready"]
    ev = registry.REGISTRY.history("nexa-tutor")[0]
    assert ev["action"] == "deployment.health" and ev["detail"]["ready"] is False
    assert not registry.REGISTRY.discover(Principal("user:s", ("student",), "user"), capability="chat")


def test_a_promoted_version_moves_the_deployment(monkeypatch):
    healthy(monkeypatch)
    monkeypatch.setenv("NEXA_AGENT_INSTANCE", "dgx-1")
    asyncio.run(presence.beat())
    reg = registry.REGISTRY
    reg.register(tutor_raw(), "user:engineer")
    for s in ("review", "approved", "active"):
        reg.set_state("nexa-tutor", "1.1.0", s, "user:admin", approved_by="Mr. Chandana")
    asyncio.run(presence.beat())
    assert [d["version"] for d in reg.deployments("nexa-tutor")] == ["1.1.0"]
    acts = [e["action"] for e in reg.history("nexa-tutor")]
    assert "deployment.removed" in acts


def test_registry_audit_trail_covers_every_change(client):
    eng, admin = hdr(["ai_engineer"], "user:engineer"), hdr(["admin"], "user:admin")
    client.post("/v1/registry/agents", json=tutor_raw(), headers=eng)
    client.put("/v1/registry/agents/nexa-tutor/versions/1.1.0/state", json={"state": "review"}, headers=admin)
    client.post("/v1/registry/agents/nexa-tutor/disable", json={"reason": "drill"}, headers=admin)
    events = client.get("/v1/registry/history?limit=3", headers=hdr(["auditor"])).json()["events"]
    assert [(e["action"], e["actor"], e["version"]) for e in events] == [
        ("agent.disabled", "user:admin", None),
        ("version.state", "user:admin", "1.1.0"),
        ("version.registered", "user:engineer", "1.1.0"),
    ]
    assert events[1]["detail"]["old"] == "draft" and events[1]["detail"]["new"] == "review"


def test_content_fingerprint_ignores_only_the_lifecycle_record():
    a = mf.AgentManifest.model_validate(tutor_raw())
    raw = copy.deepcopy(tutor_raw())
    raw["lifecycle"].update(state="approved", approved_by="Mr. Chandana", approved_on="2026-11-18")
    b = mf.AgentManifest.model_validate(raw)
    assert a.content_fingerprint() == b.content_fingerprint() and a.fingerprint() != b.fingerprint()
    raw["prompt"]["system"] += "!"
    assert mf.AgentManifest.model_validate(raw).content_fingerprint() != a.content_fingerprint()


def test_api_gateway_forwards_discovery_and_registry_calls(monkeypatch):
    """Clients reach the registry through the public gateway; the runtime
    enforces the permissions."""
    import httpx

    from v1.api_gateway import main as api
    healthy(monkeypatch)
    monkeypatch.setenv("NEXA_AGENT_INSTANCE", "dgx-1")
    asyncio.run(presence.beat())
    monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=app))
    gw = TestClient(api.app)
    r = gw.get("/v1/registry/discover?capability=chat", headers=hdr(["student"]))
    assert r.status_code == 200 and "nexa-tutor" in {a["agent_id"] for a in r.json()["agents"]}
    assert gw.post("/v1/registry/agents/nexa-tutor/disable", json={"reason": "x"},
                   headers=hdr(["student"])).status_code == 403
    r = gw.post("/v1/registry/agents/nexa-tutor/disable", json={"reason": "drill"}, headers=hdr(["admin"]))
    assert r.status_code == 200 and registry.AGENTS["nexa-tutor"].enabled is False
    assert gw.get("/v1/registry/discover").status_code == 401
    assert gw.get("/v1/registry/agents/..%2F..%2Fhealth", headers=hdr(["student"])).status_code in (400, 404, 422)
