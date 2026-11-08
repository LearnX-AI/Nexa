"""Run the M2 ingestion pipeline over the real curriculum corpus (NEXA-M2-002).

    python v1/tools/knowledge_corpus.py --out /tmp/nexa-corpus [--limit N]

What it does:
1. Builds the two formats the repository has no real samples of, from real
   curriculum material: an image-only (scanned) PDF rasterised from syllabus
   pages, with one deliberately degraded page, and a PPTX deck built from a
   Grade 12 lesson note.
2. Ingests every PDF and DOCX at the repository root plus those two files,
   with a manifest carrying the governance fields.
3. Proves idempotency: re-submits everything (no new jobs, sources or chunks),
   then reprocesses one file with a different chunking config (new run, old
   chunk set deactivated, still one source version).
4. Writes corpus_report.json, the chunk records, and validated sample source
   records.

Originals are read from the repository and are never modified.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import io
import json
import os
import re
import sys
import time
from urllib.parse import quote

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from v1.knowledge.ingestion.blobstore import BlobStore  # noqa: E402
from v1.knowledge.ingestion.pipeline import Pipeline  # noqa: E402
from v1.knowledge.ingestion.registry import Registry  # noqa: E402

APPROVER = "content-team (NEXA-M0-004 baseline set; owner confirmation pending)"


def pdf_created(path: str) -> dt.date | None:
    import pypdf
    meta = pypdf.PdfReader(path).metadata or {}
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", str(meta.get("/CreationDate") or ""))
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def docx_modified(path: str) -> dt.date | None:
    import docx
    m = docx.Document(path).core_properties.modified
    return m.date() if m else None


def build_scanned_pdf(src: str, pages: list[int], out: str) -> str:
    """Rasterise real pages to an image-only PDF. The last page is degraded
    (60 dpi, 1.5 degree skew, blur) so low-confidence OCR detection is exercised."""
    import pypdfium2
    from PIL import ImageFilter

    pdf = pypdfium2.PdfDocument(src)
    images = []
    for n, p in enumerate(pages):
        page = pdf[p - 1]
        last = n == len(pages) - 1
        img = page.render(scale=(60 if last else 150) / 72).to_pil().convert("L")
        page.close()
        if last:
            img = img.rotate(1.5, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.8))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        buf.seek(0)
        from PIL import Image
        images.append(Image.open(buf).convert("RGB"))
    pdf.close()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:], resolution=150)
    return out


def build_pptx_from_lesson(src_docx: str, out: str) -> str:
    """A teaching deck from a real lesson note: Heading 1 sections become slides,
    their paragraphs become bullets, the first table becomes a table slide, and
    the section's first paragraph becomes the speaker notes."""
    import docx
    from pptx import Presentation
    from pptx.util import Inches

    d = docx.Document(src_docx)
    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = "Transcription and Translation"
    title_slide.placeholders[1].text = "Grade 12 Biology - Genetics and Molecular Biology"
    sections: list[tuple[str, list[str]]] = []
    for p in d.paragraphs:
        if not p.text.strip():
            continue
        if p.style.name.startswith("Heading 1"):
            sections.append((p.text.strip(), []))
        elif sections:
            sections[-1][1].append(p.text.strip())
    for title, paras in sections[:8]:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        tf = s.placeholders[1].text_frame
        bullets = [x for x in paras if len(x) < 260][:5] or paras[:2]
        tf.text = bullets[0] if bullets else ""
        for b in bullets[1:]:
            para = tf.add_paragraph()
            para.text = b
            para.level = 1 if b.endswith(".") and len(b) < 120 else 0
        if paras:
            s.notes_slide.notes_text_frame.text = paras[0]
    if d.tables:
        t = d.tables[0]
        rows = [[c.text.strip() for c in r.cells] for r in t.rows][:8]
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = "Key Terms"
        shape = s.shapes.add_table(len(rows), len(rows[0]), Inches(0.5), Inches(1.5), Inches(9), Inches(4))
        for i, r in enumerate(rows):
            for j, val in enumerate(r[: len(rows[0])]):
                shape.table.cell(i, j).text = val
    prs.save(out)
    return out


def manifest_for(path: str) -> dict:
    name = os.path.basename(path)
    if name.lower().endswith(".pdf") and not name.startswith("scanned-"):
        eff = pdf_created(path)
    elif name.lower().endswith(".docx"):
        eff = docx_modified(path)
    else:
        eff = None
    eff = eff or dt.date(2023, 1, 1)
    md = {"approval_status": "approved", "approved_by": APPROVER, "effective_from": eff.isoformat(),
          "version_label": str(eff.year), "source_uri": f"repo://LearnX-AI/nexa/nexa-v1-core/{quote(name)}"}
    if name.startswith("scanned-"):
        md.update({"source_id": "png-ndoe:stem-physics-syllabus-gr12-t1-scan", "source_type": "official_syllabus",
                   "subject": "Physics", "grade_levels": [12], "term": 1, "scope": "national",
                   "publisher": "PNG National Department of Education", "access_classification": "learner",
                   "title": "STEM Physics Syllabus, Grade 12, Term 1 (scanned extract)",
                   "source_uri": "repo://LearnX-AI/nexa/nexa-v1-core/STEMPhysics-Term1-Syllubus-Gr12.pdf#scan"})
    if name.endswith(".pptx"):
        md.update({"source_id": "school:demo-high:teacher-deck-transcription", "source_type": "teacher_material",
                   "subject": "Biology", "grade_levels": [12], "scope": "school",
                   "jurisdiction": {"country": "PG", "school_id": "demo-high"},
                   "access_classification": "teacher_only", "approval_status": "approved",
                   "approved_by": "head-of-science (demo)", "title": "Transcription and Translation (teacher deck)",
                   "source_uri": "file://generated/teacher-deck-transcription.pptx"})
    return {"metadata": md}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="only the first N repository files")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    gen = os.path.join(args.out, "generated")
    os.makedirs(gen, exist_ok=True)

    files = sorted(glob.glob(os.path.join(ROOT, "*.pdf"))) + sorted(glob.glob(os.path.join(ROOT, "s*.docx")))
    if args.limit:
        files = files[: args.limit]
    scanned = build_scanned_pdf(os.path.join(ROOT, "STEMPhysics-Term1-Syllubus-Gr12.pdf"), [16, 17, 18],
                                os.path.join(gen, "scanned-STEMPhysics-Term1-Syllabus-Gr12-p16-18.pdf"))
    lesson = next(f for f in glob.glob(os.path.join(ROOT, "s05_*.docx")))
    deck = build_pptx_from_lesson(lesson, os.path.join(gen, "teacher-deck-transcription.pptx"))
    files += [scanned, deck]

    reg = Registry(f"sqlite:///{os.path.join(args.out, 'registry.db')}")
    pipe = Pipeline(reg, BlobStore(os.path.join(args.out, "originals")))
    results = []
    t0 = time.time()
    for f in files:
        name = os.path.basename(f)
        with open(f, "rb") as fh:
            data = fh.read()
        t = time.time()
        job, created = pipe.submit(name, data, manifest_for(f), "tool:knowledge_corpus")
        if job["state"] == "queued":
            job = pipe.process(job["job_id"])
        run = reg.get_run(job["run_id"]) if job.get("run_id") else None
        results.append({
            "file": name, "job_id": job["job_id"], "state": job["state"], "stage": job["stage"],
            "error": job["error_code"], "detail": job["error_detail"], "warnings": job["warnings"],
            "version_id": job["version_id"], "seconds": round(time.time() - t, 2),
            "extractor": run and run["extractor_version"], "ocr": run and run["ocr"],
            "stats": run and run["stats"], "quality": run and run["quality"],
        })
        q = (run or {}).get("quality") or {}
        print(f"{job['state']:10} {name[:60]:60} chunks={q.get('chunks')} cov={q.get('coverage')} "
              f"{results[-1]['seconds']}s", flush=True)

    before = reg.counts()
    resubmits = [pipe.submit(os.path.basename(f), open(f, "rb").read(), manifest_for(f))[1] for f in files]
    after_resubmit = reg.counts()
    first = files[0]
    alt = manifest_for(first)
    alt["chunking"] = {"target_tokens": 256, "max_tokens": 400}
    rj, _ = pipe.submit(os.path.basename(first), open(first, "rb").read(), alt)
    rj = pipe.process(rj["job_id"])
    after_reprocess = reg.counts()
    vid = rj["version_id"]
    idempotency = {
        "counts_after_first_pass": before,
        "resubmit_created_any_job": any(resubmits),
        "counts_after_resubmit": after_resubmit,
        "reprocess_file": os.path.basename(first),
        "reprocess_state": rj["state"],
        "counts_after_reprocess": after_reprocess,
        "versions_of_reprocessed_source": len(reg.versions_of(reg.get_source(vid)["source_id"])),
        "runs_of_reprocessed_version": len(reg.runs_of(vid)),
        "active_chunks_reprocessed_version": len(reg.chunks_of(vid)),
        "all_chunks_reprocessed_version": len(reg.chunks_of(vid, active_only=False)),
    }
    # Put the default config back as the active set for the benchmark.
    rj2, _ = pipe.submit(os.path.basename(first), open(first, "rb").read(), manifest_for(first))
    pipe.retry(rj2["job_id"], {"approval_status": "approved"})

    with open(os.path.join(args.out, "corpus_report.json"), "w") as fh:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "total_seconds": round(time.time() - t0, 1), "files": results,
                   "idempotency": idempotency}, fh, indent=2, default=str)
    chunks = []
    for s in reg.list_sources("active"):
        chunks += reg.chunks_of(s["version_id"])
    with open(os.path.join(args.out, "chunks.jsonl"), "w") as fh:
        for c in chunks:
            fh.write(json.dumps(c, default=str) + "\n")
    samples = [s["metadata"] for s in reg.list_sources("active")]
    with open(os.path.join(args.out, "source_records.json"), "w") as fh:
        json.dump(samples, fh, indent=2, default=str)
    print(json.dumps(idempotency, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
