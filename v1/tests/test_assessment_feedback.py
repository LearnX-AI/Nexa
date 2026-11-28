"""NEXA-M4-005 Assessment & Feedback: the offline evaluation, the contracts of
the two agents, and the school-server check against a fake school Knowledge
Service."""
from __future__ import annotations

import json

import httpx
import pytest

from v1.tools import assessment_eval as ae
from v1.tools.resilience.sandbox import default_results


@pytest.fixture(scope="module")
def report():
    from v1.tools.resilience_eval import _isolate
    _isolate()
    from v1.tools.resilience import assessment_cases as ac
    return ac.report()


@pytest.mark.parametrize("category", ["quality", "alignment", "boundary", "consistency", "safety", "integration"])
def test_every_category_passes(report, category):
    failed = [f"{r['id']}: {r['detail']}" for r in report["results"] if r["category"] == category and not r["passed"]]
    assert report["categories"][category]["cases"] > 0
    assert not failed, failed


def test_no_safety_invariant_violated(report):
    assert report["meets"] and report["violations"] == []


def test_two_agents_one_for_each_audience():
    from v1.agent_runtime import manifest as mf
    ps = mf.load_proposals()
    t, s = ps["nexa-assessment-feedback"], ps["nexa-formative-feedback"]
    assert [a.value for a in t.purpose.audience] == ["teacher"] and t.pedagogy.profile == "assessment"
    assert [a.value for a in s.purpose.audience] == ["student"] and s.pedagogy.profile == "tutor"
    assert s.permissions.knowledge.access_classes == ["learner"] and s.safety.policy == "nexa.safety.student/1"
    assert "restricted" not in t.permissions.knowledge.access_classes
    assert {x.name for x in s.tools} == {"knowledge_answer"}


def test_teaching_document_kinds_cover_every_task():
    tool = json.load(open("v1/tool_gateway/tools/teaching_document.json"))
    from v1.tool_gateway import handlers
    from v1.tools.resilience.assessment_cases import TASKS
    assert set(TASKS) <= set(tool["input_schema"]["properties"]["kind"]["enum"]) <= set(handlers._KINDS)


def _fake_school(teacher_only_for_students=False, protected=False):
    """A school Knowledge Service: answers, queries and status."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        if req.url.path == "/v1/edge/status":
            return httpx.Response(200, json={"version": 3})
        if req.url.path == "/v1/knowledge/query":
            return httpx.Response(200, json={"results": default_results(body)})
        q = body.get("question", "")
        if "Grade 9" in q or "Grade 8" in q:
            return httpx.Response(200, json={"status": "no_evidence", "citations": []})
        stype = "teacher_guide" if teacher_only_for_students and body.get("audience") == "student" else \
            ("assessment" if protected else "student_guide")
        return httpx.Response(200, json={"status": "grounded", "answer": "From the curriculum [S1].", "citations": [
            {"marker": "S1", "used": True, "provenance": {"title": "Guide", "source_type": stype, "chunk_id": "c"}}]})
    return httpx.MockTransport(handler)


def test_school_check_meets_on_a_good_school_server(tmp_path, monkeypatch, capsys):
    # The sandbox curriculum has a course for four of the six scenarios: keep those.
    monkeypatch.setattr(ae, "SCENARIOS", [s for s in ae.SCENARIOS if (s[0], s[1]) in
                                          {("Biology", "11"), ("Biology", "12"), ("Chemistry", "11"), ("Physics", "12")}])
    rc = ae.school("http://school.test", "pomnsoe", str(tmp_path), transport=_fake_school())
    out = capsys.readouterr().out
    assert rc == 0, out
    rep = json.loads(next(tmp_path.glob("nexa-assessment-eval-school-*.json")).read_text())
    assert rep["metrics"]["mapped"] == 1.0 and rep["metrics"]["no_protected"] == 1.0
    assert rep["rows"][0]["outcomes"] == ["11.1.1"]


@pytest.mark.parametrize("kw", [{"teacher_only_for_students": True}, {"protected": True}])
def test_school_check_fails_on_teacher_only_for_students_or_protected_material(tmp_path, monkeypatch, capsys, kw):
    monkeypatch.setattr(ae, "SCENARIOS", ae.SCENARIOS[:1])
    rc = ae.school("http://school.test", "pomnsoe", str(tmp_path), transport=_fake_school(**kw))
    assert rc == 1 and "NOT MET" in capsys.readouterr().out
