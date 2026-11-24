"""NEXA-M4-001: the Teaching Intelligence Policy. One versioned policy, composed
by the runtime into every education agent's calls, never duplicated in prompts."""
import asyncio
import copy
import json
import os

import pytest

from v1.agent_runtime import gateway_client, manifest as mf, pedagogy, registry, runtime
from v1.agent_runtime.pedagogy import contract as pc
from v1.identity.principal import Principal
from v1.tools.resilience import sandbox
from v1.tools.resilience.sandbox import STUDENT, TEACHER, Knowledge, Model, World

POLICY = pedagogy.get("1.0.0")
SCHEMA = os.path.join(os.path.dirname(mf.__file__), "schema", "nexa-pedagogy-1.0.schema.json")
COVERS = {  # what NEXA-M4-001 asks the policy to cover -> its section (or grade bands)
    "age/grade-aware explanation style": "grade_bands", "curriculum grounding rules": "grounding",
    "step-by-step teaching and hint policy": "steps", "direct answer vs scaffolding": "answer_mode",
    "misconception handling": "misconceptions", "uncertainty and abstention": "uncertainty",
    "examples for PNG": "context", "student vs teacher tone and permissions": "roles",
    "human escalation boundaries": "escalation", "source and citation expectations": "citations",
    "language policy and Tok Pisin decision points": "language",
}


# ---------------------------------------------------------------- the policy

def test_the_policy_covers_everything_the_task_asks_for():
    sections = {s.id for s in POLICY.sections}
    for need, sid in COVERS.items():
        assert sid == "grade_bands" and POLICY.grade_bands or sid in sections, need
    assert {d.id for d in POLICY.decision_points} >= {"LANG-D1", "LANG-D2", "LANG-D3"}
    assert all(d.owner and d.current for d in POLICY.decision_points)
    assert [g for b in POLICY.grade_bands for g in b.grades] == list(range(7, 13))


def test_released_versions_are_immutable(monkeypatch):
    assert pedagogy.versions() == ["1.0.0"]
    pc.get.cache_clear()
    monkeypatch.setattr(pc, "_released", lambda: {"1.0.0": "sha256:" + "0" * 64})
    with pytest.raises(pedagogy.PedagogyError, match="changed after release"):
        pc.get("1.0.0")
    pc.get.cache_clear()


def test_an_unreleased_version_cannot_be_used():
    with pytest.raises(pedagogy.PedagogyError, match="not a released version"):
        pedagogy.get("9.9.9")


def test_published_schema_matches_the_contract():
    with open(SCHEMA) as fh:
        assert json.load(fh) == pedagogy.json_schema()


def test_the_policy_contract_refuses_inconsistent_policies():
    raw = json.load(open(pc._path("1.0.0")))
    bad = copy.deepcopy(raw)
    bad["profiles"]["tutor"]["sections"].append("nope")
    with pytest.raises(ValueError, match="unknown sections"):
        pedagogy.Policy.model_validate(bad)
    bad = copy.deepcopy(raw)
    bad["sections"][0]["rules"].append(dict(bad["sections"][1]["rules"][0]))
    with pytest.raises(ValueError, match="duplicate rule ids"):
        pedagogy.Policy.model_validate(bad)
    bad = copy.deepcopy(raw)
    bad["status"] = "approved"
    with pytest.raises(ValueError, match="approved_by"):
        pedagogy.Policy.model_validate(bad)


# ---------------------------------------------------------------- composition

def _ids(c):
    import re
    return set(re.findall(r"\[(TP-[A-Z]+-\d{2})\]", c.text))


def test_students_and_teachers_get_their_own_rules():
    s = pedagogy.compose(POLICY, profile="tutor", audience="student", grade=11)
    t = pedagogy.compose(POLICY, profile="tutor", audience="teacher", grade=11)
    assert {"TP-MODE-02", "TP-MODE-03", "TP-STEP-02", "TP-ROLE-02", "TP-ESC-01"} <= _ids(s)
    assert not {"TP-MODE-05", "TP-ROLE-03", "TP-ESC-04"} & _ids(s)
    assert {"TP-MODE-05", "TP-ROLE-03", "TP-ESC-04", "TP-CITE-03"} <= _ids(t)
    assert not {"TP-MODE-03", "TP-ROLE-02"} & _ids(t)
    assert s.record["audience"] == "student" and t.record["audience"] == "teacher"


def test_audience_comes_from_the_roles():
    assert pedagogy.compose(POLICY, profile="tutor", roles=["student"]).record["audience"] == "student"
    assert pedagogy.compose(POLICY, profile="tutor", roles=["teacher"]).record["audience"] == "teacher"
    assert pedagogy.compose(POLICY, profile="tutor", roles=["teacher", "student"]).record["audience"] == "student"
    assert pedagogy.compose(POLICY, profile="tutor", roles=[]).record["audience"] == "student"


@pytest.mark.parametrize("grade,band", [(7, "g7_8"), ("8", "g7_8"), ("Grade 10", "g9_10"), (11, "g11_12"),
                                        ("12", "g11_12"), (None, "g9_10"), ("unknown", "g9_10")])
def test_the_grade_picks_the_style(grade, band):
    c = pedagogy.compose(POLICY, profile="tutor", audience="student", grade=grade)
    assert c.record["band"] == band
    assert all(r.id in c.text for r in next(b for b in POLICY.grade_bands if b.id == band).rules)


def test_internal_calls_get_only_the_format_rules():
    c = pedagogy.compose(POLICY, profile="internal", audience="student", grade=11)
    assert _ids(c) == {"TP-FMT-01", "TP-FMT-02"} and c.record["band"] is None


def test_tok_pisin_input_is_noted_for_the_model():
    scr = pedagogy.screen(POLICY, "Mi laik save olsem wanem photosynthesis i wok?")
    assert scr == {"language": "tok_pisin"}
    c = pedagogy.compose(POLICY, profile="tutor", audience="student", language=scr["language"])
    assert "apply TP-LANG-02" in c.text and c.record["language"] == "tok_pisin"
    assert pedagogy.screen(POLICY, "What is photosynthesis?") == {}


@pytest.mark.parametrize("text,hit", [
    ("I want to kill myself", "self_harm"), ("i keep thinking about suicide", "self_harm"),
    ("I want to die", "self_harm"), ("I am self harming again", "self_harm"),
    ("My uncle keeps hitting me", "abuse"), ("I don't feel safe at home", "abuse"),
    ("What is suicide prevention?", None), ("Explain apoptosis: the cell kills itself", None),
    ("I am dying to know about volcanoes", None), ("What is self-harm?", None),
    ("Write a lesson on child abuse prevention", None), ("How do plants die in drought?", None),
])
def test_escalation_triggers_are_first_person_and_specific(text, hit):
    assert pedagogy.screen(POLICY, text).get("escalation") == hit


def test_composition_is_deterministic_and_recorded():
    a = pedagogy.compose(POLICY, profile="planning", audience="teacher", grade=9)
    b = pedagogy.compose(POLICY, profile="planning", audience="teacher", grade=9)
    assert a.text == b.text and a.record["digest"] == b.record["digest"]
    assert a.record | {"digest": None} == {"policy": "nexa-teaching-policy", "version": "1.0.0", "status": "proposed",
                                           "profile": "planning", "audience": "teacher", "band": "g9_10", "grade": 9,
                                           "rules": a.record["rules"], "language": None, "digest": None}


# ---------------------------------------------------------------- manifests

def _proposal(aid):
    return json.load(open(os.path.join(mf.PROPOSALS_DIR, f"{aid}.json")))


def test_every_education_agent_references_the_policy():
    current = mf.load_dir()
    proposals = mf.load_proposals(current=current)
    for aid, m in current.items():
        if not mf.is_education_agent(m):
            continue
        latest = proposals.get(aid, m)
        assert latest.pedagogy is not None, aid
        assert latest.pedagogy.version in pedagogy.versions()
        assert pc.restated(POLICY, latest.prompt.system) == [], aid
        assert (aid, m.identity.version) in mf.LEGACY_WITHOUT_PEDAGOGY or m.pedagogy is not None


def test_the_legacy_list_is_closed():
    assert mf.LEGACY_WITHOUT_PEDAGOGY == {("nexa-tutor", "1.0.0"), ("nexa-precise", "1.0.0"),
                                          ("nexa-curriculum-tutor", "0.1.0"), ("nexa-curriculum-tutor", "0.2.0"),
                                          ("nexa-lesson-planner", "0.1.0")}


def test_a_new_education_agent_version_must_reference_the_policy():
    raw = _proposal("nexa-tutor")
    raw.pop("pedagogy")
    assert "must reference the NEXA Teaching Intelligence Policy" in \
        mf.pedagogy_problems(mf.AgentManifest.model_validate(raw))[0]


def test_an_agent_prompt_may_not_restate_the_policy():
    raw = _proposal("nexa-tutor")
    raw["prompt"]["system"] += " Explain step by step and cite [S1]."
    p = mf.pedagogy_problems(mf.AgentManifest.model_validate(raw))
    assert p and "restates the teaching policy" in p[0]


@pytest.mark.parametrize("ref,msg", [({"version": "2.0.0"}, "not a released version"),
                                     ({"profile": "internal"}, "not an agent profile"),
                                     ({"profile": "nope"}, "not an agent profile")])
def test_the_reference_names_a_released_version_and_an_agent_profile(ref, msg):
    raw = _proposal("nexa-tutor")
    raw["pedagogy"].update(ref)
    assert msg in " ".join(mf.pedagogy_problems(mf.AgentManifest.model_validate(raw)))


def test_proposals_are_registered_for_review_and_the_current_version_keeps_serving():
    reg = registry.reload(":memory:")
    assert [(v["version"], v["state"]) for v in reg.versions("nexa-tutor")] == [("1.0.0", "active"),
                                                                                ("1.1.0", "review")]
    assert registry.AGENTS["nexa-tutor"].version == "1.0.0"
    assert registry.AGENTS["nexa-tutor"].pedagogy_version is None
    reg.set_state("nexa-tutor", "1.1.0", "approved", "user:admin", approved_by="Mr. Chandana")
    reg.set_state("nexa-tutor", "1.1.0", "active", "user:admin")
    assert (registry.AGENTS["nexa-tutor"].version, registry.AGENTS["nexa-tutor"].pedagogy_profile) == \
        ("1.1.0", "tutor")


def test_legacy_fingerprints_are_unchanged_by_the_new_field():
    # A version registered before the field existed keeps its fingerprint, so a
    # deployed registry still recognises it as unchanged on the next seed.
    import hashlib
    m = mf.load_dir()["nexa-tutor"]
    assert "pedagogy" not in m.canonical()
    before = {k: v for k, v in m.model_dump(mode="json").items() if k != "pedagogy"}
    assert m.fingerprint() == "sha256:" + hashlib.sha256(
        json.dumps(before, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_the_helpline_is_not_treated_as_personal_data():
    from v1.agent_runtime.policy import pdp
    text = POLICY.escalation_response["student"]
    assert pc.personal_phones(pdp.PHONE, text) == []
    assert pc.personal_phones(pdp.PHONE, "call me on 7123 4567") == ["7123 4567"]


# ---------------------------------------------------------------- runtime

@pytest.fixture
def activated(monkeypatch):
    reg = registry.reload(":memory:")
    for aid in ("nexa-tutor", "nexa-precise"):
        reg.set_state(aid, "1.1.0", "approved", "user:admin", approved_by="Test approver")
        reg.set_state(aid, "1.1.0", "active", "user:admin")
    seen = []

    async def chat(model, messages, temperature=None, max_tokens=None, trace_id=None):
        seen.append(messages)
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(gateway_client, "chat", chat)
    yield seen
    registry.reload(":memory:")


def _run(name, roles, text, teaching=None):
    return asyncio.run(runtime.run_agent(name, Principal("user:t", tuple(roles), "user"),
                                         [{"role": "user", "content": text}], teaching=teaching))


def test_the_runtime_adds_the_policy_to_the_agents_own_prompt(activated):
    r = _run("nexa-tutor", ["student"], "What is osmosis?", {"grade": "11"})
    system = activated[-1][0]["content"]
    assert system.startswith(registry.AGENTS["nexa-tutor"].system_prompt)
    assert "NEXA Teaching Intelligence Policy 1.0.0 (tutor)" in system and "[TP-MODE-03]" in system
    assert r.x_nexa["pedagogy"]["band"] == "g11_12" and r.x_nexa["pedagogy"]["audience"] == "student"
    _run("nexa-tutor", ["teacher"], "What is osmosis?")
    assert "[TP-MODE-05]" in activated[-1][0]["content"] and "[TP-MODE-03]" not in activated[-1][0]["content"]


def test_an_internal_call_gets_the_internal_profile(activated):
    r = _run("nexa-precise", ["student"], "Reply with one word.", {"internal": True})
    assert r.x_nexa["pedagogy"]["profile"] == "internal"
    assert "[TP-STEP-01]" not in activated[-1][0]["content"]


def test_a_legacy_version_runs_unchanged(monkeypatch):
    registry.reload(":memory:")
    seen = []

    async def chat(model, messages, temperature=None, max_tokens=None, trace_id=None):
        seen.append(messages)
        return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(gateway_client, "chat", chat)
    r = _run("nexa-tutor", ["student"], "What is osmosis?")
    assert seen[-1][0]["content"] == registry.AGENTS["nexa-tutor"].system_prompt
    assert "pedagogy" not in r.x_nexa


def test_distress_is_answered_by_the_policy_without_a_model_call(activated, capsys):
    r = _run("nexa-tutor", ["student"], "I want to kill myself")
    assert activated == [] and r.model == "none"
    assert "7150 8000" in r.output and "trust" in r.output
    assert r.x_nexa["escalation"] == "self_harm"
    log = capsys.readouterr().out
    assert "pedagogy.escalation:nexa-tutor" in log and "kill" not in log


# ---------------------------------------------------------------- workflows

def test_a_workflow_escalates_before_any_step():
    with World(Model(), Knowledge(status="grounded")) as w:
        r = w.start("nexa-wf-assistant", STUDENT, {"message": "my step father keeps hitting me"})
        trace = w.trace(r["run_id"])
        assert r["status"] == "succeeded" and "7150 8000" in r["output"]
        assert [t["kind"] for t in trace] == ["run.started", "pedagogy.escalation", "run.succeeded"]
        assert w.model.calls == []
    assert sandbox.LAST_VIOLATIONS == []


def test_workflows_pass_grade_and_internal_use_to_the_policy():
    from v1.tools.resilience.scenarios import TWO
    with World(Model(intent="general", coordinator=TWO), Knowledge(), proposals=True) as w:
        r = w.start("nexa-wf-assistant", STUDENT, {"message": "Who invented the telephone?"})
        calls = {t["step"]: t["detail"].get("pedagogy") for t in w.trace(r["run_id"]) if t["kind"] == "agent.call"}
        assert calls["classify"]["profile"] == "internal"
        assert calls["general"]["profile"] == "tutor" and calls["general"]["audience"] == "student"
        r = w.start("nexa-wf-lesson-explainer", TEACHER, {"topic": "Photosynthesis", "grade": "8"})
        peds = [t["detail"]["pedagogy"] for t in w.trace(r["run_id"]) if t["kind"] == "agent.call"]
        assert {p["profile"] for p in peds if p} >= {"internal"}
        assert any(p and p["band"] == "g7_8" and p["audience"] == "teacher" for p in peds)
    assert sandbox.LAST_VIOLATIONS == []


def test_the_resilience_and_compatibility_suites_still_pass_with_the_proposals():
    from v1.tools.resilience import compat
    with World(Model(intent="curriculum"), Knowledge(status="grounded"), proposals=True) as w:
        r = w.start("nexa-wf-assistant", STUDENT, {"message": "What is photosynthesis?"})
        assert w.path(r["run_id"])[-1] == "cited"
    rep = compat.offline_report()
    assert rep["meets"], [x for x in rep["results"] if not x["passed"]]


# ---------------------------------------------------------------- API

def test_the_policy_is_readable_and_the_preview_is_for_engineers():
    from fastapi.testclient import TestClient

    from v1.agent_runtime.main import app
    from v1.identity import tokens
    registry.reload(":memory:")
    c = TestClient(app)

    def h(roles):
        return {"Authorization": f"Bearer {tokens.issue_token('user:x', roles, 'user', 300)}"}
    body = c.get("/v1/teaching-policy", headers=h(["student"])).json()
    assert body["versions"][0]["version"] == "1.0.0" and body["versions"][0]["digest"].startswith("sha256:")
    full = c.get("/v1/teaching-policy/1.0.0", headers=h(["teacher"])).json()
    assert full["policy"]["id"] == "nexa-teaching-policy" and len(full["policy"]["sections"]) == 11
    assert c.get("/v1/teaching-policy/9.9.9", headers=h(["teacher"])).status_code == 404
    assert c.get("/v1/teaching-policy/1.0.0/compose", headers=h(["teacher"])).status_code == 403
    p = c.get("/v1/teaching-policy/1.0.0/compose?audience=teacher&grade=9", headers=h(["ai_engineer"])).json()
    assert p["record"]["band"] == "g9_10" and "[TP-MODE-05]" in p["text"]
    assert c.get("/v1/teaching-policy").status_code in (401, 403)
