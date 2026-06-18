from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Image,
    Table,
    TableStyle,
)


def _safe(value) -> str:
    return "" if value is None else str(value)


def _page_footer(canvas, doc) -> None:
    """Render footer with branding and page number."""
    canvas.saveState()
    # Draw top border line
    canvas.setStrokeColor(colors.HexColor("#0F4C81"))
    canvas.setLineWidth(1.2)
    canvas.line(doc.leftMargin, 20 * mm, doc.pagesize[0] - doc.rightMargin, 20 * mm)
    
    # Footer text
    canvas.setFont("Helvetica-Bold", 10)
    canvas.setFillColor(colors.HexColor("#0F4C81"))
    canvas.drawString(doc.leftMargin, 14 * mm, "Nexa")
    
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#5E6C84"))
    canvas.drawString(doc.leftMargin + 20 * mm, 14 * mm, "Lecture Short Notes")
    
    # Page number on the right
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#5E6C84"))
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 14 * mm, f"Page {doc.page}")
    
    canvas.restoreState()


def build_pdf_report(manifest: Dict, chunk_rows: List[Dict], output_path: Path) -> None:
    """Render a reusable lecture short-notes template PDF."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    # ensure template styles exist here (PointText, ChunkLabel used below)
    styles.add(
        ParagraphStyle(
            name="PointText",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#243B53"),
            leading=12,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ChunkLabel",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            leading=13,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="TitleCenter",
            parent=styles["Title"],
            alignment=TA_CENTER,
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=28,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="LogoText",
            parent=styles["Heading3"],
            textColor=colors.white,
            fontName="Helvetica-Bold",
            fontSize=14,
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionHeader",
            parent=styles["Heading2"],
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            spaceBefore=8,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodyMuted",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#52606D"),
            leading=14,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="MiniHeader",
            parent=styles["Heading3"],
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            spaceBefore=4,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="PointText",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#243B53"),
            leading=14,
            leftIndent=8,
            spaceAfter=2,
        )
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=22 * mm,
        title="Nexa Lecture Short Notes Template",
        author="Nexa Pipeline",
    )

    story: List = []

    # Title / Module area
    story.append(Paragraph("Topic:", styles["MiniHeader"]))
    story.append(Paragraph("_______________________________________________________________", styles["PointText"]))
    story.append(Spacer(1, 4 * mm))

    # Two-column row: Learning objective & Key terms
    obj_box = [Paragraph("<b>Learning Objective</b>", styles["ChunkLabel"]), Paragraph("What students should know or be able to do after the lesson.", styles["PointText"]) ]
    terms_box = [Paragraph("<b>Key Terms</b>", styles["ChunkLabel"]), Paragraph("Term 1 — meaning\nTerm 2 — meaning\nTerm 3 — meaning", styles["PointText"]) ]

    table_data = [
        [obj_box, terms_box],
        [
            [Paragraph("<b>Notes</b>", styles["ChunkLabel"]), Paragraph(("\n" * 18)),],
            [Paragraph("<b>Example / Diagram</b>", styles["ChunkLabel"]), Paragraph(("\n" * 8)),],
        ],
        [Paragraph("<b>Quick Check</b>", styles["ChunkLabel"]), Paragraph("One short question or exit prompt.", styles["PointText"])],
    ]

    # Normalize table rows: each cell should be a single flowable; combine lists into a single Paragraph container
    def _cell_from_list(items):
        if isinstance(items, list):
            parts = []
            for it in items:
                if hasattr(it, 'getPlainText'):
                    parts.append(it.getPlainText())
                else:
                    parts.append(str(it))
            text = "\n\n".join(parts)
            return Paragraph(text, styles["PointText"])
        return items

    # Build final table rows
    final_rows = []
    # first row: objective / key terms
    final_rows.append([_cell_from_list(obj_box), _cell_from_list(terms_box)])
    # second row: notes full area and example
    notes_cell = Paragraph("\n" * 12, styles["PointText"])
    example_cell = Paragraph("\n" * 6, styles["PointText"])
    final_rows.append([notes_cell, example_cell])
    # third row: quick check
    final_rows.append([Paragraph("<b>Quick Check</b>", styles["ChunkLabel"]), Paragraph("One short question or exit prompt.", styles["PointText"])])

    tbl = Table(final_rows, colWidths=[(210 - 36) * mm / 2, (210 - 36) * mm / 2])
    tbl.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BCCCDC")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E6EEF6")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7FBFF")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    story.append(tbl)

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)


def build_lecture_pdf(lecture: Dict, output_path: Path) -> None:
    """Render a single-lesson short-notes PDF using the unified template.

    Expected lecture dict keys: title, learning_objective, notes, key_terms, example, quick_check
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="TitleCenter",
            parent=styles["Title"],
            alignment=TA_CENTER,
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="FieldLabel",
            parent=styles["Heading3"],
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=13,
            spaceBefore=4,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="FieldValue",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#243B53"),
            leading=14,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="PointText",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#243B53"),
            leading=12,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="ChunkLabel",
            parent=styles["BodyText"],
            textColor=colors.HexColor("#102A43"),
            fontName="Helvetica-Bold",
            leading=13,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="LogoText",
            parent=styles["Heading3"],
            textColor=colors.white,
            fontName="Helvetica-Bold",
            fontSize=14,
            alignment=TA_CENTER,
        )
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=lecture.get("title", "Lecture Short Notes"),
        author="Nexa Pipeline",
    )

    # Creative header colors and styles
    header_bg = colors.HexColor("#0F4C81")
    accent = colors.HexColor("#2E8BC0")

    story: List = []

    # Header: "Nexa" text logo and title
    title_text = lecture.get("title") or "Lecture Short Notes"
    nexa_logo = Paragraph("<b style='font-size: 18'>Nexa</b>", styles["LogoText"])
    
    header_cells = [[nexa_logo, Paragraph(title_text, styles["TitleCenter"])]]
    header_cols = [35 * mm, (210 - 36) * mm - 35 * mm]
    
    header_table = Table(header_cells, colWidths=header_cols)
    header_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), header_bg),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 16),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ])
    )
    story.append(header_table)
    story.append(Spacer(1, 6 * mm))

    # Metadata row: Module / Date / Duration
    meta = lecture.get("meta", {}) if isinstance(lecture.get("meta"), dict) else {}
    module = meta.get("module") or "Module"
    date = meta.get("date") or "Date"
    duration = meta.get("duration") or "Duration"
    meta_row = Table(
        [[Paragraph(f"<b>Module:</b> {module}", styles["PointText"]), Paragraph(f"<b>Date:</b> {date}", styles["PointText"]), Paragraph(f"<b>Duration:</b> {duration}", styles["PointText"]) ]],
        colWidths=[(210 - 36) * mm * 0.33, (210 - 36) * mm * 0.34, (210 - 36) * mm * 0.33],
    )
    meta_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(meta_row)
    story.append(Spacer(1, 4 * mm))

    # Objective and Key Terms cards (shaded)
    obj = lecture.get("learning_objective") or ""
    terms = lecture.get("key_terms") or ""
    obj_card = Table([[Paragraph("<b>Learning Objective</b>", styles["ChunkLabel"])],[Paragraph(obj, styles["FieldValue"]) ]], colWidths=[(210 - 36) * mm / 2])
    obj_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    terms_card = Table([[Paragraph("<b>Key Terms</b>", styles["ChunkLabel"])],[Paragraph(terms, styles["FieldValue"]) ]], colWidths=[(210 - 36) * mm / 2])
    terms_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F7FB")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    cards = Table([[obj_card, terms_card]], colWidths=[(210 - 36) * mm / 2, (210 - 36) * mm / 2])
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(cards)
    story.append(Spacer(1, 6 * mm))

    # Notes area: ruled lines
    notes = lecture.get("notes") or ""
    notes_title = Paragraph("<b>Notes</b>", styles["ChunkLabel"])
    # Create a block with ruled lines and optional prefilled text paragraphs
    notes_flow = []
    if notes.strip():
        notes_flow.append(Paragraph(notes, styles["FieldValue"]))
        notes_flow.append(Spacer(1, 3 * mm))
    # add 10 ruled lines
    ruled_lines = []
    for _ in range(10):
        ruled_lines.append(Paragraph("\u2014" * 120, styles["PointText"]))
    notes_block = [notes_title] + ruled_lines

    # Example box
    example = lecture.get("example") or ""
    example_block = [Paragraph("<b>Example / Diagram</b>", styles["ChunkLabel"])]
    if example.strip():
        example_block.append(Paragraph(example, styles["FieldValue"]))
    else:
        # small ruled area for sketching
        for _ in range(6):
            example_block.append(Paragraph("\u2014" * 60, styles["PointText"]))

    notes_table = Table([[notes_block, example_block]], colWidths=[(210 - 36) * mm * 0.65, (210 - 36) * mm * 0.35])
    notes_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E9F7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    story.append(notes_table)
    story.append(Spacer(1, 6 * mm))

    # Quick check
    qc = lecture.get("quick_check") or ""
    story.append(Paragraph("Quick Check", styles["ChunkLabel"]))
    story.append(Paragraph(qc, styles["FieldValue"]))

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)


def build_blank_lecture_template(output_path: Path) -> None:
    """Render a blank creative lecture short-notes template (no hard-coded content).

    Produces the same creative layout but leaves fields empty for manual/AI fill-in.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(name="TitleCenter", parent=styles["Title"], alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=20, leading=24)
    )
    styles.add(ParagraphStyle(name="ChunkLabel", parent=styles["BodyText"], fontName="Helvetica-Bold", textColor=colors.HexColor("#102A43"), leading=13))
    styles.add(ParagraphStyle(name="FieldValue", parent=styles["BodyText"], textColor=colors.HexColor("#243B53"), leading=12))
    styles.add(ParagraphStyle(name="PointText", parent=styles["BodyText"], textColor=colors.HexColor("#243B53"), leading=12))
    styles.add(ParagraphStyle(name="LogoText", parent=styles["Heading3"], textColor=colors.white, fontName="Helvetica-Bold", fontSize=14, alignment=TA_CENTER))

    doc = SimpleDocTemplate(str(output_path), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm, title="Lecture Short Notes Template", author="Nexa Pipeline")

    header_bg = colors.HexColor("#0F4C81")
    accent = colors.HexColor("#2E8BC0")

    story: List = []
    # header with "Nexa" text logo
    nexa_logo = Paragraph("<b style='font-size: 18'>Nexa</b>", styles["LogoText"])
    
    header_cells = [[nexa_logo, Paragraph("Lecture Short Notes", styles["TitleCenter"])]]
    header_cols = [35 * mm, (210 - 36) * mm - 35 * mm]
    
    header_table = Table(header_cells, colWidths=header_cols)
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), header_bg),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 16),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6 * mm))

    # Empty metadata placeholders
    meta_row = Table([[Paragraph("<b>Module:</b>", styles["PointText"]), Paragraph("<b>Date:</b>", styles["PointText"]), Paragraph("<b>Duration:</b>", styles["PointText"]) ]], colWidths=[(210 - 36) * mm * 0.33, (210 - 36) * mm * 0.34, (210 - 36) * mm * 0.33])
    meta_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(meta_row)
    story.append(Spacer(1, 6 * mm))

    # Objective and Key Terms empty cards
    obj_card = Table([[Paragraph("<b>Learning Objective</b>", styles["ChunkLabel"])],[Paragraph("", styles["FieldValue"]) ]], colWidths=[(210 - 36) * mm / 2])
    obj_card.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    terms_card = Table([[Paragraph("<b>Key Terms</b>", styles["ChunkLabel"])],[Paragraph("", styles["FieldValue"]) ]], colWidths=[(210 - 36) * mm / 2])
    terms_card.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F7FB")), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    cards = Table([[obj_card, terms_card]], colWidths=[(210 - 36) * mm / 2, (210 - 36) * mm / 2])
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(cards)
    story.append(Spacer(1, 8 * mm))

    # Notes area with ruled lines (blank)
    notes_title = Paragraph("<b>Notes</b>", styles["ChunkLabel"]) 
    ruled_lines = [Paragraph("\u2014" * 120, styles["PointText"]) for _ in range(8)]
    notes_block = [notes_title] + ruled_lines

    example_title = Paragraph("<b>Example / Diagram</b>", styles["ChunkLabel"]) 
    example_lines = [Paragraph("\u2014" * 60, styles["PointText"]) for _ in range(4)]
    example_block = [example_title] + example_lines

    notes_table = Table([[notes_block, example_block]], colWidths=[(210 - 36) * mm * 0.65, (210 - 36) * mm * 0.35])
    notes_table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E9F7")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(notes_table)
    story.append(Spacer(1, 6 * mm))

    # Quick check blank
    story.append(Paragraph("Quick Check", styles["ChunkLabel"]))
    story.append(Paragraph("", styles["FieldValue"]))

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)