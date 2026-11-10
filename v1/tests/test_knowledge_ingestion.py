"""NEXA-M2-002: validation, extraction, OCR, idempotent and versioned ingestion."""
from __future__ import annotations

import pathlib

import pytest

from v1.knowledge import config
from v1.knowledge.ingestion import extractors, ocr
from v1.knowledge.ingestion.model import BlockKind, PageStatus
from v1.knowledge.ingestion.pipeline import Pipeline, sha256
from v1.knowledge.ingestion.validation import (DOCX, PDF, PPTX, ValidationError, scan_sensitive_text,
                                               validate_file)

from .knowledge_fixtures import (APPROVED, lesson_manifest, make_docx, make_pptx, make_zip, minimal_pdf,
                                 scanned_pdf_from_text)

ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_SYLLABUS = ROOT / "STEMChem-Term1-Syllubus-Gr12.pdf"
OCR = pytest.mark.skipif(not ocr.available(), reason="tesseract with eng data not installed")


# ---- validation ----

def test_accepts_real_types():
    assert validate_file("a.docx", make_docx()).mime_type == DOCX
    assert validate_file("a.pptx", make_pptx()).mime_type == PPTX
    assert validate_file("a.pdf", minimal_pdf()).mime_type == PDF


@pytest.mark.parametrize("name, data, code", [
    ("a.pdf", b"", "empty"),
    ("a.exe", b"MZ\x90\x00", "unsupported_type"),
    ("a.docx", minimal_pdf(), "type_mismatch"),
    ("a.pdf", b"PK\x03\x04garbage", "type_mismatch"),
    ("a.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, "unsupported_type"),
    ("a.pdf", minimal_pdf(b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>"), "active_content"),
    ("a.pdf", minimal_pdf(b"/OpenAction << /S /J#61vaScript >>"), "active_content"),
    ("a.pdf", minimal_pdf(b"/Launch << /F (cmd.exe) >>"), "active_content"),
    ("a.pdf", minimal_pdf(b"/Encrypt 5 0 R"), "encrypted"),
])
def test_rejects_bad_files(name, data, code):
    with pytest.raises(ValidationError) as e:
        validate_file(name, data)
    assert e.value.code == code


def _office(extra: dict) -> bytes:
    base = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document/>"}
    base.update(extra)
    return make_zip(base)


@pytest.mark.parametrize("extra, code", [
    ({"word/vbaProject.bin": b"macro"}, "active_content"),
    ({"../evil.txt": b"x"}, "path_traversal"),
    ({"word/_rels/settings.xml.rels":
      b'<Relationships><Relationship Type="http://x/attachedTemplate" Target="http://evil/t.dotm" '
      b'TargetMode="External"/></Relationships>'}, "active_content"),
    ({"word/bomb.xml": b"\x00" * 30_000_000}, "zip_bomb"),
])
def test_rejects_hostile_office_files(extra, code):
    with pytest.raises(ValidationError) as e:
        validate_file("a.docx", _office(extra))
    assert e.value.code == code


def test_size_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 100)
    with pytest.raises(ValidationError, match="too_large"):
        validate_file("a.pdf", minimal_pdf() + b" " * 200)


def test_required_av_rejects_without_scanner(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_AV", True)
    monkeypatch.setattr(config, "AV_SCANNER", "none")
    with pytest.raises(ValidationError, match="av_unavailable"):
        validate_file("a.pdf", minimal_pdf())


def test_sensitive_text_scan():
    key = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE..."  # built at runtime so scanners ignore it
    assert "private_key" in scan_sensitive_text(key)[0]
    assert "aws_access_key" in scan_sensitive_text("id " + "AKIA" + "ABCDEFGHIJKLMNOP" + " here")[0]
    assert "assigned_secret" in scan_sensitive_text("pass" + "word = " + "not-a-real-one")[0]
    roster = " ".join(f"student{i}@school.pg" for i in range(12))
    assert any(b.startswith("personal_data") for b in scan_sensitive_text(roster)[0])
    blocking, warnings = scan_sensitive_text("Contact the teacher at teacher@school.pg")
    assert not blocking and warnings
    assert scan_sensitive_text("Photosynthesis makes ATP, NADPH and oxygen.") == ([], [])


# ---- extractors ----

def test_docx_extraction_structure_and_pages():
    doc = extractors.extract(DOCX, make_docx())
    blocks = [b for p in doc.pages for b in p.blocks]
    kinds = [b.kind for b in blocks]
    assert BlockKind.HEADING in kinds and BlockKind.LIST_ITEM in kinds and BlockKind.TABLE in kinds
    table = next(b for b in blocks if b.kind == BlockKind.TABLE)
    assert table.rows[0] == ["Feature", "Leading Strand", "Lagging Strand"]
    assert len(doc.pages) == 2 and doc.pages[0].basis == "rendered_estimate"
    assert any(b.kind == BlockKind.TABLE for b in doc.pages[1].blocks)  # after the page break
    assert "5′→3′" in " ".join(b.text for b in blocks)  # symbols survive
    assert doc.extractor_version.startswith("python-docx ")


def test_pptx_extraction_slides_tables_notes():
    doc = extractors.extract(PPTX, make_pptx())
    assert [p.number for p in doc.pages] == [1, 2, 3] and doc.pages[0].basis == "slide"
    s2 = doc.pages[1].blocks
    assert s2[0].kind == BlockKind.HEADING and s2[0].text == "How It Works"
    assert any(b.kind == BlockKind.NOTES and "galvanometer" in b.text for b in s2)
    assert any(b.kind == BlockKind.TABLE and b.rows == [["Wired", "Inductive"], ["Plug needed", "No plug"]]
               for b in doc.pages[2].blocks)


def test_lms_html_extraction():
    html = ("<h2>Biogas</h2><p>Biogas is made by <b>anaerobic</b> digestion.</p><script>alert(1)</script>"
            "<ul><li>Methane</li><li>Carbon dioxide</li></ul>"
            "<table><tr><th>Pro</th><th>Con</th></tr><tr><td>Renewable</td><td>Slow when cold</td></tr></table>")
    doc = extractors.extract_lms_html(html)
    kinds = [(b.kind, b.text) for b in doc.pages[0].blocks]
    assert (BlockKind.HEADING, "Biogas") in kinds
    assert (BlockKind.PARAGRAPH, "Biogas is made by anaerobic digestion.") in kinds
    assert not any("alert" in t for _, t in kinds)
    assert doc.pages[0].blocks[-1].rows == [["Pro", "Con"], ["Renewable", "Slow when cold"]]


@pytest.mark.skipif(not REAL_SYLLABUS.exists(), reason="curriculum corpus not present")
def test_real_pdf_pages_and_printed_labels():
    doc = extractors.extract(PDF, REAL_SYLLABUS.read_bytes())
    assert len(doc.pages) == 25 and doc.overall_status == "succeeded"
    assert doc.pages[9].label == "VIII"  # physical page 10 is printed VIII
    assert all(p.basis == "physical" for p in doc.pages)


@OCR
def test_scanned_pdf_is_ocred_with_engine_recorded():
    data = scanned_pdf_from_text(["MODULE 12.1: INDUCTIVE CHARGING", "",
                                  "Inductive charging transfers energy through a magnetic field",
                                  "between two coils without a plug or cable."])
    doc = extractors.extract(PDF, data)
    p = doc.pages[0]
    assert p.method == "ocr" and p.status in (PageStatus.OCR, PageStatus.LOW_CONFIDENCE)
    assert "magnetic field" in p.raw_text and p.ocr_confidence > 50
    assert doc.ocr["engine"] == "tesseract" and doc.ocr["config"] == "--oem 1 --psm 3"


@OCR
def test_blank_scan_is_empty_not_failed():
    doc = extractors.extract(PDF, scanned_pdf_from_text([]))
    assert doc.pages[0].status == PageStatus.EMPTY and doc.overall_status == "failed"


# ---- pipeline ----

def test_ingest_is_idempotent_and_records_provenance():
    pipe = Pipeline()
    data = make_docx(extra_paragraphs=6)
    job, created = pipe.submit("lesson.docx", data, lesson_manifest(), "user:t")
    assert created and job["state"] == "queued"
    job = pipe.process(job["job_id"])
    assert job["state"] == "succeeded" and job["stage"] == "index" and job["version_id"]
    counts = pipe.registry.counts()
    again, created2 = pipe.submit("renamed.docx", data, lesson_manifest(), "user:t")
    assert not created2 and again["job_id"] == job["job_id"]
    assert pipe.process(job["job_id"])["state"] == "succeeded"
    assert pipe.registry.counts() == counts
    run = pipe.registry.get_run(job["run_id"])
    assert run["pipeline_version"] and run["normalizer_version"] and run["chunker_version"]
    assert run["extractor_version"].startswith("python-docx") and run["quality"]["coverage"] >= 0.98
    src = pipe.registry.get_source(job["version_id"])
    assert src["metadata"]["content_checksum"] == sha256(data)
    assert src["metadata"]["source_uri"].startswith("edunex://")


def test_reprocess_with_new_config_creates_run_not_duplicates():
    pipe = Pipeline()
    data = make_docx(extra_paragraphs=12)
    j1 = pipe.process(pipe.submit("l.docx", data, lesson_manifest())[0]["job_id"])
    first = {c["chunk_id"] for c in pipe.registry.chunks_of(j1["version_id"])}
    m2 = lesson_manifest()
    m2["chunking"] = {"target_tokens": 96, "max_tokens": 128, "min_tokens": 20}
    j2 = pipe.process(pipe.submit("l.docx", data, m2)[0]["job_id"])
    assert j2["version_id"] == j1["version_id"]
    assert len(pipe.registry.versions_of("edunex:cm9001")) == 1
    assert len(pipe.registry.runs_of(j1["version_id"])) == 2
    active = {c["chunk_id"] for c in pipe.registry.chunks_of(j1["version_id"])}
    assert active.isdisjoint(first)  # old chunk set deactivated, not mixed in
    every = pipe.registry.chunks_of(j1["version_id"], active_only=False)
    assert len(every) == len(first) + len(active)


def test_new_version_supersedes_and_same_bytes_elsewhere_is_duplicate():
    pipe = Pipeline()
    v1 = pipe.process(pipe.submit("l.docx", make_docx(), lesson_manifest())[0]["job_id"])
    v2 = pipe.process(pipe.submit("l.docx", make_docx(extra_paragraphs=2),
                                  lesson_manifest(effective_from="2025-01-01"))[0]["job_id"])
    assert v2["version_id"] != v1["version_id"]
    assert pipe.registry.get_source(v1["version_id"])["status"] == "superseded"
    assert pipe.registry.chunks_of(v1["version_id"]) == []
    assert any("superseded" in w for w in v2["warnings"])
    dup = pipe.process(pipe.submit("copy.docx", make_docx(), lesson_manifest("cm7777"))[0]["job_id"])
    assert dup["state"] == "duplicate" and dup["version_id"] == v1["version_id"]


def test_metadata_failure_is_visible_and_retryable_with_patch():
    pipe = Pipeline()
    job = pipe.process(pipe.submit("notes.docx", make_docx(), {})[0]["job_id"])
    assert job["state"] == "failed" and job["stage"] == "metadata" and job["retryable"]
    assert "source_id" in job["error_detail"]
    fixed = pipe.retry(job["job_id"], lesson_manifest()["metadata"])
    assert fixed["state"] == "succeeded" and fixed["version_id"].startswith("edunex:cm9001@")


def test_corrupt_file_fails_at_extract_and_bounded_retries(monkeypatch):
    monkeypatch.setattr(config, "MAX_JOB_ATTEMPTS", 2)
    pipe = Pipeline()
    broken = _office({"word/document.xml": b"<not-closed"})
    job = pipe.process(pipe.submit("b.docx", broken, lesson_manifest())[0]["job_id"])
    assert job["state"] == "failed" and job["stage"] == "extract" and job["retryable"]
    job = pipe.retry(job["job_id"])
    assert job["attempts"] == 2 and job["state"] == "failed"
    job = pipe.retry(job["job_id"])
    assert job["error_code"] == "max_attempts" and not job["retryable"]


def test_hostile_and_sensitive_files_are_quarantined():
    pipe = Pipeline()
    job, _ = pipe.submit("x.pdf", minimal_pdf(b"/JS (x)"), {})
    assert job["state"] == "quarantined" and job["stage"] == "validate" and not job["retryable"]
    assert pipe.blobs.get(job["checksum"]) is None  # rejected bytes are not stored
    leak = make_docx(body=[("Normal", "Server config: api_key = " + "sk-" + "proj-" + "x" * 40)], with_table=False)
    job = pipe.process(pipe.submit("leak.docx", leak, lesson_manifest())[0]["job_id"])
    assert job["state"] == "quarantined" and job["stage"] == "scan" and job["error_code"] == "sensitive_content"
    assert pipe.registry.counts()["chunks"] == 0
    assert pipe.retry(job["job_id"])["state"] == "quarantined"


def test_pending_sources_are_chunked_but_marked_pending():
    pipe = Pipeline()
    m = lesson_manifest()
    m["metadata"].pop("approval_status")
    job = pipe.process(pipe.submit("l.docx", make_docx(), m)[0]["job_id"])
    src = pipe.registry.get_source(job["version_id"])
    assert src["metadata"]["approval_status"] == "pending_review"
    assert pipe.registry.chunks_of(job["version_id"])


def test_lms_push_ingests_html():
    pipe = Pipeline()
    item = {"lms": {"system": "edunex", "course_id": "stem-bio-gr12", "item_id": "cm555", "revision": "7"},
            "title": "Biogas Production", "format": "html",
            "body": "<h1>Biogas</h1><p>Biogas is produced by anaerobic digestion of organic waste. It is "
                    "mostly methane and carbon dioxide.</p>",
            "metadata": {"subject": "Biology", "grade_levels": [12], "scope": "national",
                         "access_classification": "learner", **APPROVED}}
    job, created = pipe.submit_lms(item)
    job = pipe.process(job["job_id"])
    assert job["state"] == "succeeded"
    src = pipe.registry.get_source(job["version_id"])["metadata"]
    assert src["source_id"] == "edunex:cm555" and src["source_uri"].endswith("?rev=7")
    assert pipe.submit_lms(item)[1] is False


def test_image_uploads_follow_pillow_safety_gate(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    monkeypatch.setattr(config, "IMAGE_UPLOADS", "off")
    with pytest.raises(ValidationError, match="Pillow 13"):
        validate_file("scan.png", png)
    monkeypatch.setattr(config, "IMAGE_UPLOADS", "force")
    assert validate_file("scan.png", png).mime_type == "image/png"


def test_docx_text_box_fallback_not_doubled():
    import docx
    from docx.oxml import parse_xml
    d = docx.Document()
    p = d.add_paragraph()
    p._p.append(parse_xml(
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<mc:Choice Requires="wps"><w:r><w:t>Figure 3: Locating a gene</w:t></w:r></mc:Choice>'
        '<mc:Fallback><w:r><w:t>Figure 3: Locating a gene</w:t></w:r></mc:Fallback></mc:AlternateContent>'))
    import io
    buf = io.BytesIO()
    d.save(buf)
    doc = extractors.extract(DOCX, buf.getvalue())
    texts = [b.text for pg in doc.pages for b in pg.blocks]
    assert texts == ["Figure 3: Locating a gene"]


def test_resubmitted_job_has_the_same_shape_as_a_new_one():
    """A re-submit returns the existing job; it must be the decoded job, not the
    raw row (the corpus tool failed with KeyError 'warnings' on the DGX)."""
    pipe = Pipeline()
    data = make_docx()
    first = pipe.process(pipe.submit("l.docx", data, lesson_manifest())[0]["job_id"])
    again, created = pipe.submit("l.docx", data, lesson_manifest())
    assert not created and again == pipe.registry.get_job(first["job_id"])
    assert isinstance(again["warnings"], list) and isinstance(again["retryable"], bool)
    assert "warnings_json" not in again and "manifest_json" not in again
