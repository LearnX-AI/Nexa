"""NEXA-M4-004 Knowledge Service changes: the grade guard, premise correction
and the LMS item listing used to re-scope a site's content."""
from __future__ import annotations

import datetime as dt

from v1.identity import tokens
from v1.knowledge import config, curriculum, grounding
from v1.knowledge import main as km

from .knowledge_fixtures import make_docx
from .test_knowledge_grounding import CASES, _upload, svc  # noqa: F401 - fixture
from .test_lms_sync import H, SITE, ask, event, lms_hits, push, user
from .test_lms_sync import svc as lms_svc  # noqa: F401 - fixture

BIO12 = CASES[1]  # DNA replication teacher guide, Biology Grade 12: "which enzyme unwinds the DNA double helix"


def test_grades_named():
    assert curriculum.grades_named("In Grade 7 English, what is a noun?") == [7]
    assert curriculum.grades_named("Compare gr. 11 and Year 12 physics, then grade 11 again") == [11, 12]
    assert curriculum.grades_named("What is 12 times 7?") == []
    assert curriculum.grades_named("grade 120 or year 2026") == []


def test_question_naming_another_grade_gets_no_answer_from_this_grade(svc, make_token, auth):  # noqa: F811 - fixture
    c, calls, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), BIO12[2])
    ok = c.post("/v1/knowledge/answer", headers=auth(student),
                json={"question": "In Grade 12 Biology, " + BIO12[3]}).json()
    assert ok["status"] == "grounded"
    assert any("searched only Grade 12" in n for n in ok["notes"])
    n = len(calls)
    r = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "In Grade 7 Science, " + BIO12[3]}).json()
    assert r["status"] == "no_evidence" and r["citations"] == [] and len(calls) == n  # no model call
    assert any("searched only Grade 7" in x for x in r["notes"])
    assert any("no approved Grade 7 material" in x for x in r["notes"])
    # Without a grade in the question nothing changes.
    plain = c.post("/v1/knowledge/answer", headers=auth(student), json={"question": BIO12[3]}).json()
    assert plain["status"] == "grounded" and not any("searched only" in x for x in plain["notes"])
    # An explicit grade filter wins over the grade named in the question.
    f = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "In Grade 7, " + BIO12[3], "filters": {"grades": [12]}}).json()
    assert f["status"] == "grounded"


def test_grade_guard_can_be_switched_off(svc, make_token, auth, monkeypatch):  # noqa: F811 - fixture
    c, _, _ = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), BIO12[2])
    monkeypatch.setattr(config, "GRADE_GUARD", False)
    r = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "In Grade 7 Science, " + BIO12[3]}).json()
    assert r["status"] == "grounded"


def test_false_premise_is_corrected_from_the_passages(svc, make_token, auth):  # noqa: F811 - fixture
    c, calls, replies = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), BIO12[2])
    q = "Why does DNA polymerase unwind the DNA double helix?"
    replies.extend(["INSUFFICIENT_EVIDENCE",
                    "PREMISE_CORRECTION: It is not DNA polymerase that unwinds the double helix; helicase "
                    "unwinds it [S1]."])
    r = c.post("/v1/knowledge/answer", headers=auth(student), json={"question": q}).json()
    assert r["evidence"]["level"] == "strong", r["evidence"]
    assert r["status"] in ("grounded", "grounded_weak") and r["source_kind"] == "curriculum"
    assert r["answer"].startswith("It is not DNA polymerase") and "PREMISE_CORRECTION" not in r["answer"]
    assert grounding.NOTE_PREMISE in r["notes"]
    assert "the passages contradict an assumption in the question" in r["evidence"]["reasons"]
    assert any(ci["used"] for ci in r["citations"])
    assert calls[-1][0]["content"].startswith(grounding.PREMISE_PROMPT[:40])


def test_premise_check_refuses_when_not_contradicted_or_uncited(svc, make_token, auth, monkeypatch):  # noqa: F811 - fixture
    c, calls, replies = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), BIO12[2])
    q = "Why does DNA polymerase unwind the DNA double helix?"
    # The second look also finds nothing to correct.
    replies.extend(["INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"])
    r = c.post("/v1/knowledge/answer", headers=auth(student), json={"question": q}).json()
    assert r["status"] == "no_evidence" and r["answer"] == grounding.FALLBACK_NO_EVIDENCE
    # A "correction" that cites nothing is withheld.
    replies.extend(["INSUFFICIENT_EVIDENCE", "PREMISE_CORRECTION: Helicase does that."])
    r = c.post("/v1/knowledge/answer", headers=auth(student), json={"question": q}).json()
    assert r["status"] == "no_evidence"
    assert "the correction cited no passage and was withheld" in r["evidence"]["reasons"]
    # A reply without the marker is not taken as a correction.
    replies.extend(["INSUFFICIENT_EVIDENCE", "Helicase unwinds it [S1]."])
    assert c.post("/v1/knowledge/answer", headers=auth(student),
                  json={"question": q}).json()["status"] == "no_evidence"
    # Switched off: one model call and the refusal.
    monkeypatch.setattr(config, "PREMISE_CORRECTION", False)
    n = len(calls)
    replies.extend(["INSUFFICIENT_EVIDENCE", "PREMISE_CORRECTION: x [S1]."])
    r = c.post("/v1/knowledge/answer", headers=auth(student), json={"question": q}).json()
    assert r["status"] == "no_evidence" and len(calls) == n + 1
    replies.clear()


def test_premise_check_needs_strong_evidence(svc, make_token, auth, monkeypatch):  # noqa: F811 - fixture
    c, calls, replies = svc
    teacher, student = make_token(["teacher"]), make_token(["student"])
    _upload(c, auth, teacher, "bio.docx", make_docx(), BIO12[2])
    monkeypatch.setattr(config, "EVIDENCE_THRESHOLDS", {"default": {"min": 0.05, "strong": 0.99}})
    monkeypatch.setattr(config, "EVIDENCE_COVERAGE_STRONG", 1.01)
    n = len(calls)
    replies.extend(["INSUFFICIENT_EVIDENCE", "PREMISE_CORRECTION: x [S1]."])
    r = c.post("/v1/knowledge/answer", headers=auth(student),
               json={"question": "what happens to the strands at the fork"}).json()
    assert r["evidence"]["level"] == "weak" and r["status"] == "no_evidence" and len(calls) == n + 1
    replies.clear()


def test_lms_items_listing_and_rescope_by_replaying_events(lms_svc):  # noqa: F811
    c, _ = lms_svc
    e = event()
    assert push(c, [e]).json()["results"][0]["state"] == "applied"
    admin_old = tokens.issue_token("admin:x", ["platform_admin"], "user", 300)
    items = c.get("/v1/knowledge/lms/items", headers=H(admin_old)).json()["items"]
    assert len(items) == 1 and items[0]["state"] == "active" and items[0]["last_event_id"] == e["event_id"]
    assert c.get("/v1/knowledge/lms/items", headers=H(user(["student"], "pg-ncd-0042"))).status_code == 403
    stored = c.get(f"/v1/knowledge/lms/events/{e['event_id']}", headers=H(admin_old)).json()
    assert stored["payload"]["school_id"] == SITE["school_id"]
    # Re-scope: the same event, newer, sent by an administrator of the new school.
    replay = dict(stored["payload"]["event"], event_id="rescope-pomnsoe-0001",
                  occurred_at=dt.datetime.now(dt.timezone.utc).isoformat())
    admin_new = tokens.issue_token("admin:x", ["platform_admin"], "user", 300, extra={"school_id": "pomnsoe"})
    r = push(c, [replay], tok=admin_new).json()["results"][0]
    assert r["state"] == "applied"
    assert lms_hits(ask(c, user(["student"], "pomnsoe")))
    assert not lms_hits(ask(c, user(["student"], "pg-ncd-0042")))
    assert km.PIPE.registry.get_lms_item("moodle:edunex-ncd:cm100")["last_event_id"] == "rescope-pomnsoe-0001"
