"""NEXA-M4-003: the Teacher Assistant Agent. Role boundaries, curriculum
grounding, usefulness, safety and integration (v1/tools/resilience/
teacher_cases.py), and the teaching_document tool."""
import asyncio
import os

import pytest

from v1.agent_runtime import manifest as mf
from v1.agent_runtime.pedagogy import contract as pc
from v1.tool_gateway import handlers
from v1.tools.resilience import teacher_cases as tc


@pytest.fixture(scope="module")
def report():
    return tc.report()


@pytest.mark.parametrize("category", tc.CATEGORIES)
def test_every_category_passes(report, category):
    failed = [f"{r['id']}: {r['detail']} {r['violations']}" for r in report["results"]
              if r["category"] == category and not r["passed"]]
    assert report["categories"][category]["cases"] > 0
    assert not failed, failed


def test_every_task_and_several_subjects_and_grades(report):
    ids = {r["id"] for r in report["results"]}
    assert {f"usefulness.{t}" for t in tc.TASKS} <= ids and {f"grounding.{t}" for t in tc.TASKS} <= ids
    assert {g for _, g, _ in tc.SUBJECTS} == {"7", "8", "9", "10", "11", "12"}
    assert report["meets"] and report["violations"] == []


def test_the_agent_is_proposed_under_the_teaching_policy():
    m = mf.load_proposals()["nexa-teacher-assistant"]
    assert m.lifecycle.state.value == "review" and m.pedagogy.profile == "planning"
    assert m.purpose.audience == [mf.Audience.teacher] and "student" not in m.permissions.allowed_roles
    assert {t.name for t in m.tools} == {"knowledge_answer", "teaching_document"}
    assert m.memory.long_term == "none"


def _doc(**kw):
    args = {"title": "Photosynthesis", "kind": "lesson_plan", "grade": "11", "body": "## Learning objectives\n- x [S1]",
            "sources": "Plants [S1].", "run_id": "wfr_test123"} | kw
    return asyncio.run(handlers.teaching_document(args, None))


def test_the_document_carries_sources_and_provenance():
    d = _doc()["document"]
    assert d.startswith("# Photosynthesis\n\nLesson plan · Grade 11 · draft for teacher review")
    assert "## Curriculum sources" in d and "NEXA run: wfr_test123" in d and "Content digest: sha256:" in d
    assert "Reviewed and approved by" not in d
    assert "reviewed and approved by T" in _doc(reviewed_by="T")["document"]


@pytest.mark.parametrize("body", ["Call 7123 4567 about this", "Email sam@school.pg about this lesson"])
def test_the_document_refuses_contact_details(body):
    with pytest.raises(handlers.HandlerError, match="personal contact details"):
        _doc(body=body)


def test_the_document_tool_keeps_the_policy_helpline():
    assert handlers.PUBLIC_NUMBERS == pc.public_numbers()
    assert "7150 8000" in _doc(body="If a student is at risk, 1-Tok Kaunselin Helpim Lain: 7150 8000.")["document"]


def test_offline_command_writes_a_report(tmp_path, monkeypatch):
    from v1.tools import teacher_eval
    monkeypatch.setattr("v1.tools.resilience_eval._isolate", lambda: None)
    assert teacher_eval.main(["--out", str(tmp_path), "--only", "role_boundary"]) == 0
    md = [p for p in os.listdir(tmp_path) if p.endswith(".md")]
    assert md and "PASS" in (tmp_path / md[0]).read_text()


@pytest.mark.parametrize("text", ["Content digest: sha256:9e4a71234567bc0d", "NEXA run: wfr_71234567a9b8", "id 7123456x"])
def test_hashes_and_ids_are_not_phone_numbers(text):
    # A hex digest or run id that happens to hold 7xxxxxxx is not personal data,
    # and redaction must never corrupt it (a flaky V6 failure in CI, 26 Nov).
    from v1.agent_runtime.policy import pdp
    assert pdp.PHONE.search(text) is None and handlers._PHONE.search(text) is None
