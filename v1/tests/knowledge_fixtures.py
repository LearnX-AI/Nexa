"""Small document builders for the M2 knowledge tests.

Everything is generated in memory at test time; no binary fixtures are
committed. Content is modelled on the real PNG STEM material.
"""
from __future__ import annotations

import io
import zipfile
from typing import Optional


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
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


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
    return buf.getvalue()


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
