"""Small document builders for the M2 knowledge tests.

Everything is generated in memory at test time; no binary fixtures are
committed. Content is modelled on the real PNG STEM material.
"""
from __future__ import annotations

import io
import zipfile
from typing import Optional


def _stable_zip(data: bytes) -> bytes:
    """Rewrite an Office zip with fixed entry timestamps. The zip format stores
    the save time, so without this the same document built a second later has
    different bytes, and so a different checksum and source version."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(2024, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.external_attr = info.external_attr
            z.writestr(fixed, src.read(info.filename))
    return out.getvalue()


def make_docx(title: str = "DNA Replication", extra_paragraphs: int = 0, with_table: bool = True,
              page_break: bool = True, body: Optional[list[tuple[str, str]]] = None) -> bytes:
    import docx

    d = docx.Document()
    d.core_properties.title = title
    items = body or [
        ("Heading 1", "What is DNA Replication?"),
        ("Normal", "DNA replication is the biological process by which a DNA molecule produces an exact copy "
                   "of itself. It occurs during the S (Synthesis) phase of the cell cycle."),
        ("Normal", "Replication is possible because DNA consists of two complementary strands. When the strands "
                   "separate, each original strand acts as a template for a new strand in the 5′→3′ "
                   "direction."),
        ("Heading 3", "Key Enzymes"),
        ("Normal", "The main enzymes involved are listed below:"),
        ("List Paragraph", "Helicase unwinds the DNA double helix by breaking the hydrogen bonds."),
        ("List Paragraph", "Primase synthesises short RNA primers."),
        ("List Paragraph", "DNA polymerase adds complementary nucleotides."),
    ]
    for style, text in items:
        if style.startswith("Heading"):
            d.add_heading(text, level=int(style.split()[-1]))
        else:
            d.add_paragraph(text, style=style if style != "Normal" else None)
    for i in range(extra_paragraphs):
        d.add_paragraph(f"Additional explanatory paragraph number {i} about semiconservative replication, "
                        "Okazaki fragments on the lagging strand and the proofreading role of DNA polymerase. "
                        "Accurate copying keeps genetic information stable across generations.")
    if page_break:
        d.add_page_break()
    if with_table:
        d.add_heading("Leading and Lagging Strands", level=1)
        t = d.add_table(rows=3, cols=3)
        rows = [["Feature", "Leading Strand", "Lagging Strand"],
                ["Direction of Synthesis", "Toward the replication fork", "Away from the replication fork"],
                ["Synthesis Pattern", "Continuous", "Discontinuous"]]
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                t.cell(r, c).text = val
    import datetime as _dt
    d.core_properties.created = d.core_properties.modified = _dt.datetime(2024, 1, 1)
    buf = io.BytesIO()
    d.save(buf)
    return _stable_zip(buf.getvalue())


def make_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Inductive Charging"
    s.placeholders[1].text = "Grade 12 Physics, Term 2"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "How It Works"
    tf = s.placeholders[1].text_frame
    tf.text = "A changing magnetic field induces a current in a nearby coil."
    p = tf.add_paragraph()
    p.text = "Transformers use electromagnetic induction to change voltage."
    p.level = 1
    s.notes_slide.notes_text_frame.text = "Demonstrate with two coils and a galvanometer."
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Comparison"
    tbl = s.shapes.add_table(2, 2, Inches(1), Inches(1.5), Inches(6), Inches(1.5)).table
    for (r, c), v in {(0, 0): "Wired", (0, 1): "Inductive", (1, 0): "Plug needed", (1, 1): "No plug"}.items():
        tbl.cell(r, c).text = v
    buf = io.BytesIO()
    prs.save(buf)
    return _stable_zip(buf.getvalue())


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def minimal_pdf(extra: bytes = b"") -> bytes:
    return (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R " + extra + b" >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n")


def scanned_pdf_from_text(lines: list[str]) -> bytes:
    """An image-only PDF of rendered text, for the OCR path."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1700, 2200), 255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 44)
    except OSError:
        font = ImageFont.load_default(size=44)
    y = 150
    for ln in lines:
        draw.text((140, y), ln, fill=0, font=font)
        y += 80
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PDF", resolution=200)
    return buf.getvalue()


APPROVED = {"approval_status": "approved", "approved_by": "content-team", "effective_from": "2024-01-01"}


def lesson_manifest(item: str = "cm9001", **extra) -> dict:
    md = {
        "source_id": f"edunex:{item}", "source_type": "lms_content", "subject": "Biology", "grade_levels": [12],
        "title": "DNA Replication (Lesson Notes)", "scope": "national", "access_classification": "learner",
        "lms": {"system": "edunex", "course_id": "stem-biology-gr12", "item_id": item},
        "source_uri": f"edunex://course/stem-biology-gr12/item/{item}", **APPROVED,
    }
    md.update(extra)
    return {"metadata": md}


# ---- fake Model Gateway for embeddings (no network) ----

def bow_vector(text: str, dim: int = 128) -> list[float]:
    """Deterministic bag-of-words embedding: similar words, similar vectors."""
    import hashlib
    import math
    import re

    v = [0.0] * dim
    for w in re.findall(r"[a-z]{3,}", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeGateway:
    """Stands in for embeddings._gateway. Records every input text, can fail,
    and can change the model it reports, like a gateway whose registry changed."""

    def __init__(self, version: str = "nomic-embed-text", runtime: str = "ollama", dim: int = 128):
        self.version, self.runtime, self.dim = version, runtime, dim
        self.fail = False
        self.calls = 0
        self.texts: list[str] = []
        self.models: list[str] = []

    async def __call__(self, texts, model, trace_id=None):
        from v1.knowledge.embeddings import EmbeddingError

        self.calls += 1
        if self.fail:
            raise EmbeddingError("gateway 503")
        self.texts.extend(texts)
        self.models.append(model)
        return ([bow_vector(t, self.dim) for t in texts],
                {"model_version": self.version, "runtime": self.runtime, "model": model})
