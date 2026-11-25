"""NEXA-M4-002: the Student Tutor Agent. Grounding, tutoring quality, role
boundary, safety and failure modes across subjects and grades (the cases in
v1/tools/resilience/tutor_cases.py), and the runtime features it relies on."""
import json
import os

import pytest

from v1.agent_runtime import manifest as mf
from v1.agent_runtime.orchestration import workflow as wfm
from v1.tools.resilience import tutor_cases as tc
from v1.tools.resilience.sandbox import STUDENT, Knowledge, World

WF_FILE = os.path.join(os.path.dirname(wfm.__file__), "..", "workflows", "nexa-wf-student-tutor.json")


@pytest.fixture(scope="module")
def report():
    return tc.report()


@pytest.mark.parametrize("category", tc.CATEGORIES)
def test_every_category_passes(report, category):
    failed = [f"{r['id']}: {r['detail']} {r['violations']}" for r in report["results"]
              if r["category"] == category and not r["passed"]]
    assert report["categories"][category]["cases"] > 0
    assert not failed, failed


def test_representative_subjects_and_grades(report):
    grades = {int(x.split("Grade ")[1].rstrip(")")) for x in report["subjects"]}
    subjects = {x.split(" (")[0] for x in report["subjects"]}
    assert grades == {7, 8, 9, 10, 11, 12}
    assert {"biology", "chemistry", "physics", "mathematics", "geography", "english"} <= subjects
    assert report["violations"] == [] and report["meets"]


def test_the_agent_is_proposed_under_the_teaching_policy():
    m = mf.load_proposals()["nexa-student-tutor"]
    assert m.lifecycle.state.value == "review" and m.pedagogy.profile == "tutor"
    assert m.purpose.audience == [mf.Audience.student] and m.permissions.allowed_roles == ["student"]
    assert {t.name for t in m.tools} == {"knowledge_answer", "calculator"}
    assert m.safety.serves_minors and m.safety.policy == "nexa.safety.student/1"
    assert any("marking" in x for x in m.purpose.out_of_scope)


def test_cite_from_must_name_a_knowledge_step():
    raw = json.load(open(WF_FILE))
    next(s for s in raw["steps"] if s["id"] == "explain")["cite_from"] = "question"
    with pytest.raises(ValueError, match="is not a knowledge step"):
        wfm.Workflow.model_validate(raw)


def test_session_keys_of_steps_that_did_not_run_keep_their_value():
    with World(tc.tutor_model(), Knowledge(), proposals=True) as w:
        sid = w.session(STUDENT, tc.WF)
        tc._start(w, "What is photosynthesis?", session=sid)
        before = w.session_data(sid)
        tc._start(w, "Give me a practice question", session=sid)
        tc._start(w, "Can I have a hint?", session=sid)
        after = w.session_data(sid)
    assert before["explanation"] == after["explanation"]           # practice and hint did not overwrite it
    assert after["practice"].startswith("Practice:") and after["last_hint"].startswith("Hint")


def test_the_grounding_guard_is_traced():
    with World(tc.tutor_model(), Knowledge(), proposals=True) as w:
        r = tc._start(w, "What is photosynthesis?")
        g = [t["detail"] for t in w.trace(r["run_id"]) if t["kind"] == "agent.grounding"]
    assert g and g[0]["ok"] and g[0]["source"] == "lookup" and g[0]["used"] == ["[S1]"]


def test_offline_command_writes_a_report(tmp_path, monkeypatch):
    from v1.tools import tutor_eval
    monkeypatch.setattr("v1.tools.resilience_eval._isolate", lambda: None)
    assert tutor_eval.main(["--out", str(tmp_path), "--only", "safety"]) == 0
    md = [p for p in os.listdir(tmp_path) if p.endswith(".md")]
    assert md and "PASS" in (tmp_path / md[0]).read_text()
