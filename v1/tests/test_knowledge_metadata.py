"""NEXA-M2-001: source metadata contract, authority rules, PNG recognisers."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest
from pydantic import ValidationError

from v1.knowledge.authority import AUTHORITY_WEIGHTS, ClaimClass, resolve
from v1.knowledge.curriculum import detect_heading, draft_from_filename, outcome_codes
from v1.knowledge.metadata import (AuthorityTier, Locator, SourceMetadata, SourceType, json_schema)

CK = "sha256:" + "a" * 64
CK2 = "sha256:" + "b" * 64
SAMPLES = pathlib.Path(__file__).resolve().parents[1] / "knowledge" / "samples" / "source_records.sample.json"


def syllabus(**over) -> SourceMetadata:
    base = dict(source_id="png-ndoe:stem-chemistry-syllabus-gr12-t1", content_checksum=CK,
                source_type="official_syllabus", title="STEM Chemistry Syllabus, Grade 12, Term 1",
                subject="Chemistry", grade_levels=[12], term=1, scope="national",
                publisher="PNG National Department of Education", approval_status="approved",
                approved_by="content-team", access_classification="learner",
                effective_from="2023-02-10", source_uri="repo://LearnX-AI/nexa/STEMChem-Term1-Syllubus-Gr12.pdf")
    base.update(over)
    return SourceMetadata(**base)


def test_valid_record_and_derived_fields():
    m = syllabus()
    assert m.authority_tier == AuthorityTier.OFFICIAL_SYLLABUS
    assert m.version_id == "png-ndoe:stem-chemistry-syllabus-gr12-t1@" + "a" * 16
    flat = m.flat()
    assert flat["authority_tier"] == 1 and flat["grade_levels"] == "12"
    assert all(not isinstance(v, (dict, list)) for v in flat.values())


@pytest.mark.parametrize("over, msg", [
    ({"authority_tier": 3}, "does not match source_type"),
    ({"scope": "school", "jurisdiction": {"school_id": "x"}}, "national scope"),
    ({"publisher": None}, "require a publisher"),
    ({"approved_by": None}, "require approved_by"),
    ({"content_checksum": "md5:abc"}, "sha256"),
    ({"source_uri": "repo://path with spaces.pdf"}, "source_uri"),
    ({"source_uri": "https://x.org/a.pdf?token=abc123"}, "credentials"),
    ({"source_uri": "ftp://x/a.pdf"}, "source_uri"),
    ({"language": "English"}, "BCP-47"),
    ({"grade_levels": [14]}, "1 to 13"),
    ({"effective_to": "2020-01-01"}, "before effective_from"),
    ({"curriculum": {"outcome_codes": ["not-a-code"]}}, "invalid outcome codes"),
    ({"unknown_field": 1}, "Extra inputs"),
])
def test_rejected_records(over, msg):
    with pytest.raises(ValidationError, match=msg):
        syllabus(**over)


def test_scope_and_origin_rules():
    with pytest.raises(ValidationError, match="school_id"):
        syllabus(source_type="teacher_material", scope="school", publisher=None)
    ok = syllabus(source_type="teacher_material", scope="school", publisher=None, approved_by=None,
                  jurisdiction={"school_id": "demo-high"}, access_classification="teacher_only")
    assert ok.authority_tier == AuthorityTier.TEACHER_CREATED
    with pytest.raises(ValidationError, match="lms reference"):
        syllabus(source_type="lms_content", publisher=None)
    with pytest.raises(ValidationError, match="not permitted"):
        syllabus(source_type="external_reference", publisher=None,
                 external={"domain": "random-blog.example", "retrieved_at": "2026-01-01T00:00:00Z"})
    ext = syllabus(source_type="external_reference", publisher=None,
                   external={"domain": "en.wikipedia.org", "retrieved_at": "2026-01-01T00:00:00Z"})
    assert ext.authority_tier == AuthorityTier.PERMITTED_EXTERNAL


def test_retrievability_window_and_status():
    assert syllabus().is_retrievable(dt.date(2024, 1, 1))
    assert not syllabus().is_retrievable(dt.date(2022, 1, 1))
    assert not syllabus(effective_to="2023-12-31").is_retrievable(dt.date(2024, 1, 1))
    for status in ("pending_review", "draft", "superseded", "withdrawn"):
        assert not syllabus(approval_status=status).is_retrievable(dt.date(2024, 1, 1))


def test_locator_citation():
    assert Locator(page_start=10, page_label="VIII", section_path=["Module 12.1: Protein Sequencing"]).cite() \
        == "p. 10 [printed VIII], Module 12.1: Protein Sequencing"
    assert Locator(page_start=3, page_end=4, page_basis="rendered_estimate").cite() == "p. 3-4 (approx.)"
    assert Locator(page_start=2, page_basis="slide").cite() == "slide 2"


def test_json_schema_published():
    s = json_schema()
    assert s["$id"].endswith("/1.1.0.json")
    assert {"source_id", "content_checksum", "source_type", "subject", "grade_levels", "scope",
            "approval_status", "access_classification", "effective_from", "source_uri"} <= set(s["required"])


# ---- authority rules ----

def _src(sid, stype, tier_date="2023-01-01", ck=CK, **over):
    pub = "PNG National Department of Education"
    extra = {}
    if stype == "lms_content":
        extra["lms"] = {"course_id": "c", "item_id": sid.split(":")[-1]}
    if stype == "teacher_material":
        extra.update(scope="school", jurisdiction={"school_id": "demo-high"}, publisher=None)
    if stype == "external_reference":
        extra.update(external={"domain": "en.wikipedia.org", "retrieved_at": "2026-01-01T00:00:00Z"},
                     publisher=None)
    base = dict(source_id=sid, content_checksum=ck, source_type=stype, title=f"title {sid}",
                subject="Chemistry", grade_levels=[12], scope="national", publisher=pub,
                approval_status="approved", approved_by="x", access_classification="learner",
                effective_from=tier_date, source_uri=f"repo://{sid.replace(':', '/')}")
    base.update(extra)
    base.update(over)
    return SourceMetadata(**base)


def test_resolve_orders_by_tier_and_marks_outcome_authority():
    syl = _src("png:syl", "official_syllabus")
    tg = _src("png:tg", "teacher_guide", ck=CK2)
    lms = _src("edunex:cm1", "lms_content", ck="sha256:" + "c" * 64)
    ext = _src("ext:wiki", "external_reference", ck="sha256:" + "d" * 64)
    r = resolve([ext, lms, tg, syl], ClaimClass.INSTRUCTIONAL, on=dt.date(2024, 1, 1))
    assert [x.source.source_id for x in r.ranked] == ["png:syl", "png:tg", "edunex:cm1", "ext:wiki"]
    assert r.winner.source_id == "png:syl"
    assert not r.ranked[-1].authoritative  # R7 external is last resort
    out = resolve([tg, syl], ClaimClass.CURRICULUM_OUTCOME, on=dt.date(2024, 1, 1))
    assert [x.authoritative for x in out.ranked] == [True, False]  # R3
    assert resolve([tg], ClaimClass.CURRICULUM_OUTCOME, on=dt.date(2024, 1, 1)).winner is None


def test_resolve_versions_eligibility_and_local_context():
    old = _src("png:syl", "official_syllabus", "2021-01-01", ck=CK)
    new = _src("png:syl", "official_syllabus", "2023-01-01", ck=CK2)
    withdrawn = _src("png:tg", "teacher_guide", ck="sha256:" + "e" * 64, approval_status="withdrawn")
    r = resolve([old, new, withdrawn], on=dt.date(2024, 1, 1))
    assert [x.source.content_checksum for x in r.ranked] == [CK2]
    reasons = " ".join(reason for _, reason in r.excluded)
    assert "R2 superseded" in reasons and "R1 not eligible" in reasons
    school = _src("school:deck", "teacher_material", ck="sha256:" + "f" * 64)
    local = resolve([new, school], ClaimClass.LOCAL_CONTEXT, on=dt.date(2024, 1, 1))
    assert local.ranked[0].source.source_id == "school:deck"
    only_ext = resolve([_src("ext:wiki", "external_reference")], on=dt.date(2024, 1, 1))
    assert only_ext.external_only and only_ext.winner.source_id == "ext:wiki"


def test_authority_weights_are_monotonic():
    ws = [AUTHORITY_WEIGHTS[t] for t in sorted(AuthorityTier, key=lambda t: t.value)]
    assert ws == sorted(ws, reverse=True)


# ---- PNG recognisers ----

def test_draft_from_real_filenames():
    d = draft_from_filename("STEMChem-Term1-Syllubus-Gr12.pdf")
    assert d["source_type"] == SourceType.OFFICIAL_SYLLABUS and d["term"] == 1 and d["grade_levels"] == [12]
    assert d["source_id"] == "png-ndoe:stem-chemistry-syllabus-gr12-t1"
    d = draft_from_filename("STEMBio-Term2-TeacherGuide-Gr11.pdf")
    assert d["access_classification"].value == "teacher_only"
    d = draft_from_filename("s04_cm8879_12.2_Genetics and Molecular Biology_3_DNA Replication.docx")
    assert d["source_id"] == "edunex:cm8879" and d["source_type"] == SourceType.LMS_CONTENT
    assert d["curriculum"]["module_code"] == "12.2" and d["curriculum"]["topic_title"] == "DNA Replication"
    assert draft_from_filename("random notes.docx") == {"file_name": "random notes.docx"}


@pytest.mark.parametrize("line, level, field", [
    ("THEMA BIOTECHNOLOGY", 1, ("thema", "Biotechnology")),
    ("THEMA: HEALTH AND MEDICINE", 1, ("thema", "Health and Medicine")),
    ("MODULE 12.1: PROTEIN SEQUENCING", 2, ("module_code", "12.1")),
    ("MODULE 2: ORNITHOLOGY", 2, ("module_code", "2")),
    ("Topic 1: Amino Acids", 3, ("topic_title", "Amino Acids")),
])
def test_curriculum_headings(line, level, field):
    lv, _, fields = detect_heading(line)
    assert lv == level and fields[field[0]] == field[1]


@pytest.mark.parametrize("line", [
    "Thema  Thematic areas of studies",   # nomenclature table row
    "Thematic Areas of Studies",
    "THEMA MODULE TOPICS",                # table header
    "Module 12.1",                        # table cell
    "Thema: Biotechnology",               # cross-reference in prose
    "MODULE 12.1: BIOINFORMATICS .............................. 18",  # TOC line
])
def test_curriculum_heading_false_positives(line):
    assert detect_heading(line) is None


def test_outcome_codes_in_text():
    assert outcome_codes("12.1.1a: Explain amino acids. 12.1.1b and 12.1.1a again; 3.14.159 is pi") \
        == ["12.1.1a", "12.1.1b"]


def test_committed_sample_records_validate():
    records = json.loads(SAMPLES.read_text())
    assert len(records) >= 8
    kinds = {r["source_type"] for r in records}
    assert {"official_syllabus", "teacher_guide", "student_guide", "lms_content", "teacher_material"} <= kinds
    for r in records:
        m = SourceMetadata(**r)
        assert m.content_checksum.startswith("sha256:")
