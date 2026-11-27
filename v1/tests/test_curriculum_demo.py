"""NEXA-M4-004 curriculum demo pack: the automatic checks."""
from v1.tools import curriculum_demo as cd

SOURCES = [
    {"version_id": "v-bio12", "title": "DNA Structure Lesson Note", "subject": "Biology", "grade_levels": [12],
     "source_type": "lms_content"},
    {"version_id": "v-phys11", "title": "Measurement Notes", "subject": "Physics", "grade_levels": [11],
     "source_type": "lms_content"},
]


def _answer(status="grounded", version="v-bio12", text="A double helix; adenine pairs with thymine [S1].",
            checksum="abc", fabricated=()):
    return {"status": status, "source_kind": "curriculum" if status in cd.GROUNDED else "none",
            "answer": text, "confidence": "high",
            "citations": [{"marker": "S1", "used": True,
                           "provenance": {"version_id": version, "content_checksum": checksum, "chunk_id": "c1",
                                          "title": "t", "source_type": "lms_content"}}] if status in cd.GROUNDED else [],
            "integrity": {"fabricated_markers": list(fabricated)}}


CASE = next(c for c in cd.CASES if c[0] == "c03")        # Biology 12, DNA structure
NOT_COVERED = next(c for c in cd.CASES if c[0] == "n03")  # Grade 9 Mathematics
TRAP = next(c for c in cd.CASES if c[0] == "w02")         # mitochondria / photosynthesis


def test_case_set_covers_all_six_courses_and_every_category():
    covered = {(c[2], c[3]) for c in cd.CASES if c[1] == "covered"}
    assert covered == {(s, g) for s in ("Biology", "Chemistry", "Physics") for g in (11, 12)}
    assert {c[1] for c in cd.CASES} == {"covered", "not_covered", "no_answer", "incorrect"}
    assert {c[3] for c in cd.CASES if c[1] == "not_covered"} >= {7, 8, 9, 10}
    assert len({c[0] for c in cd.CASES}) == len(cd.CASES)


def test_covered_question_grounded_on_the_right_course_passes():
    r = cd.judge(CASE, _answer(), cd.source_table(SOURCES))
    assert r["passed"], r["issues"]
    assert r["sources"][0]["subject"] == "Biology" and r["sources"][0]["active"]


def test_covered_question_citing_another_subject_is_reported():
    r = cd.judge(CASE, _answer(version="v-phys11"), cd.source_table(SOURCES))
    assert not r["passed"] and "another subject or grade" in r["issues"][0]


def test_covered_question_not_grounded_is_reported():
    r = cd.judge(CASE, _answer(status="no_evidence", text=None), cd.source_table(SOURCES))
    assert not r["passed"] and "not grounded" in r["issues"][0]


def test_not_covered_grade_refused_passes_and_substitution_is_caught():
    srcs = cd.source_table(SOURCES)
    assert cd.judge(NOT_COVERED, _answer(status="no_evidence", text=None), srcs)["passed"]
    r = cd.judge(NOT_COVERED, _answer(version="v-phys11", text="Subtract 5 then divide by 3 [S1]."), srcs)
    assert not r["passed"] and "silent substitution" in r["issues"][0]


def test_false_premise_must_be_corrected():
    srcs = cd.source_table(SOURCES)
    assert cd.judge(TRAP, _answer(text="Photosynthesis happens in the chloroplast, not the mitochondrion [S1]."),
                    srcs)["passed"]
    assert not cd.judge(TRAP, _answer(text="Because mitochondria make sugar [S1]."), srcs)["passed"]


def test_source_version_and_checksum_and_fabrication_are_checked():
    srcs = cd.source_table(SOURCES)
    assert "not active" in " ".join(cd.judge(CASE, _answer(version="v-old"), srcs)["issues"])
    assert "checksum" in " ".join(cd.judge(CASE, _answer(checksum=None), srcs)["issues"])
    assert "fabricated" in " ".join(cd.judge(CASE, _answer(fabricated=["S9"]), srcs)["issues"])


def test_run_and_report_with_a_fake_service():
    def post(q):
        if "DNA" in q:
            return _answer()
        if "mitochondrion" in q:
            return _answer(text="It is the chloroplast [S1].")
        return _answer(status="no_evidence", text=None)
    rep = cd.run(post, SOURCES)
    assert rep["cases"] == len(cd.CASES)
    assert rep["fabricated_citations"] == 0 and rep["substitutions"] == 0
    assert rep["sources_cited"] == 3 and rep["sources_active"] == 3  # c02, c03, w02
    md = cd.markdown(rep, "2026-11-27 16:30", "a teacher at pomnsoe",
                     pilot=[{"n": 1, "course": "BIO11", "file": "/x/01.docx", "words": 10, "content_hits": 3,
                             "probes": 3, "etag": "0123456789abcdef", "last_modified": "2026-11-26T00:00:00Z"}])
    assert "Sign-off sheet: academic correctness" in md and "source integrity" in md
    assert "| c03 |" in md and "S3 pilot" in md
    errs = cd.run(lambda q: (_ for _ in ()).throw(RuntimeError("down")), SOURCES, cases=[CASE])
    assert errs["passed"] == 0 and errs["results"][0]["status"].startswith("error")
