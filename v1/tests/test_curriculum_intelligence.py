"""NEXA-M4-004 Curriculum Intelligence: the offline evaluation, the live
report parser and the LMS re-scope tool."""
from __future__ import annotations

import pytest

from v1.identity import tokens
from v1.tools import curriculum_eval as ce
from v1.tools import lms_rescope as rs

from .test_lms_sync import H, SITE, ask, event, lms_hits, push, sync_token, user
from .test_lms_sync import svc as lms_svc  # noqa: F401 - fixture


@pytest.fixture(scope="module")
def report():
    from v1.tools.resilience_eval import _isolate
    _isolate()
    from v1.tools.resilience import curriculum_cases as cc
    return cc.report()


@pytest.mark.parametrize("category", ["mapping", "grounding", "boundary", "safety", "integration"])
def test_every_category_passes(report, category):
    failed = [f"{r['id']}: {r['detail']}" for r in report["results"]
              if r["category"] == category and not r["passed"]]
    assert report["categories"][category]["cases"] > 0
    assert not failed, failed


def test_no_safety_invariant_violated(report):
    assert report["meets"] and report["violations"] == []
    assert set(report["tasks"]) == {"map", "alignment", "progression"}


def test_parse_mapping():
    out = ("Curriculum mapping (high confidence)\n- Grade: 11\n- Subject: Biology\n- Unit: 11.1 Genetics\n"
           "- Topic: Classical Mendelian Genetics\n- Official learning outcomes: 11.1.1, 11.1.2\n- Sources: x")
    m = ce.parse_mapping(out)
    assert m == {"grade": "11", "subject": "Biology", "unit": "11.1 Genetics",
                 "topic": "Classical Mendelian Genetics", "outcomes": ["11.1.1", "11.1.2"], "confidence": "high"}
    assert ce.parse_mapping(out.replace("11.1.1, 11.1.2", "none found"))["outcomes"] == []


def test_rescope_moves_one_schools_items_and_nothing_else(lms_svc):  # noqa: F811
    c, _ = lms_svc
    assert push(c, [event("cm100"), event("cm101", title="Respiration notes", body="<p>Mitochondria release "
                                          "energy from glucose in respiration.</p>")]).status_code == 200
    other = {**SITE, "site_id": "other-site", "school_id": "pg-other-0001"}
    push(c, [event("cm200", body="<p>Ferns reproduce with spores released from sori under their fronds.</p>")],
         tok=sync_token(site="other-site", school="pg-other-0001"), site=other)
    push(c, [event("cm300", scope="national", body="<p>Volcanoes form where tectonic plates meet.</p>")],
         tok=sync_token(national=True))
    admin = tokens.issue_token("ops:x", ["platform_admin"], "user", 300, extra={"school_id": "pomnsoe"})
    items = c.get("/v1/knowledge/lms/items", params={"state": "active"}, headers=H(admin)).json()["items"]

    def get_event(eid):
        r = c.get(f"/v1/knowledge/lms/events/{eid}", headers=H(admin))
        return r.json() if r.status_code == 200 else None
    p = rs.plan(items, get_event, "pg-ncd-0042", "pomnsoe")
    assert p["count"] == 2 and p["courses"] == {"edunex-ncd / bio12": 2}
    assert p["skipped"] == {"school is pg-other-0001": 1, "not a school-scoped upsert": 1}
    assert all(e["event_id"].startswith("rescope-") for g in p["sites"].values() for e in g["events"])
    # Nothing is sent by planning.
    assert lms_hits(ask(c, user(["student"], "pg-ncd-0042")))
    for b in rs.batches(p, size=1):
        r = c.post("/v1/knowledge/lms/events?wait=true", headers=H(admin), json=b).json()
        assert [x["state"] for x in r["results"]] == ["applied"]
    pom, old = user(["student"], "pomnsoe"), user(["student"], "pg-ncd-0042")
    assert lms_hits(ask(c, pom)) and lms_hits(ask(c, pom, "how do mitochondria release energy"), "cm101")
    assert not lms_hits(ask(c, old)) and not lms_hits(ask(c, old, "how do mitochondria release energy"), "cm101")
    assert lms_hits(ask(c, user(["student"], "pg-other-0001"), "how do ferns reproduce with spores"), "cm200")
    items = c.get("/v1/knowledge/lms/items", params={"state": "active"}, headers=H(admin)).json()["items"]
    assert rs.plan(items, get_event, "pg-ncd-0042", "pomnsoe")["count"] == 0
    # Undo: the same with the schools swapped.
    back = rs.plan(items, get_event, "pomnsoe", "pg-ncd-0042")
    assert back["count"] == 2


def test_rescope_leaves_content_shared_with_another_school(lms_svc):  # noqa: F811
    c, _ = lms_svc
    push(c, [event("cm100")])
    other = {**SITE, "site_id": "other-site", "school_id": "pg-other-0001"}
    push(c, [event("cm200")], tok=sync_token(site="other-site", school="pg-other-0001"), site=other)  # same bytes
    admin = tokens.issue_token("ops:x", ["platform_admin"], "user", 300, extra={"school_id": "pomnsoe"})
    items = c.get("/v1/knowledge/lms/items", headers=H(admin)).json()["items"]
    assert len({i["version_id"] for i in items}) == 1

    def get_event(eid):
        return c.get(f"/v1/knowledge/lms/events/{eid}", headers=H(admin)).json()
    p = rs.plan(items, get_event, "pg-ncd-0042", "pomnsoe")
    assert p["count"] == 0
    assert p["skipped"]["content shared with an item that is not moved (left unchanged)"] == 1


def test_outcome_registry_draft_from_official_syllabi_only():
    from v1.tools import outcome_registry as orx
    sources = [
        {"version_id": "v1", "title": "Biology Syllabus Grade 11", "subject": "Biology", "grade_levels": [11],
         "source_type": "official_syllabus"},
        {"version_id": "v2", "title": "Genetics Lesson Note", "subject": "Biology", "grade_levels": [11],
         "source_type": "lms_content"},
    ]
    chunks = {
        "v1": [{"text": "Unit 11.1 Genetics\n11.1.1 Explain Mendel's laws of inheritance. More text.\n"
                        "11.1.2 Use Punnett squares to predict ratios\nLeads to 12.2.1 in Grade 12.",
                "curriculum": {"module_code": "11.1", "module_title": "Genetics", "outcome_codes": ["11.1.1"]},
                "locator": {"page_label": "14"}}],
        "v2": [{"text": "11.1.9 a lesson-note code", "curriculum": {}, "locator": {}}],
    }
    reg = orx.build(sources, lambda vid: chunks[vid])
    codes = {e["code"]: e for e in reg["outcomes"]}
    assert set(codes) == {"11.1.1", "11.1.2"}  # not the lesson note's, not Grade 12's
    assert codes["11.1.1"]["statement"] == "Explain Mendel's laws of inheritance"
    assert codes["11.1.2"]["statement"] == "Use Punnett squares to predict ratios"
    assert codes["11.1.1"]["unit"] == "11.1 Genetics" and codes["11.1.1"]["sources"] == ["Biology Syllabus Grade 11, p. 14"]
    assert reg["counts"] == {"Biology Grade 11": 2} and reg["status"].startswith("draft")
    assert any("12.2.1" in i for i in reg["issues"])
    md = orx.markdown(reg, "2026-11-27")
    assert "DRAFT for CDD validation" in md and "| 11.1.1 | Explain Mendel's laws of inheritance |" in md
