"""Format extractors (NEXA-M2-002).

One function per format, each returning an ExtractedDocument. Library versions
are recorded so the same bytes plus the same versions always give the same
text. Extractors never raise for a single bad page: the page is marked FAILED
and the rest of the document continues, so failures are visible per page.
"""
from __future__ import annotations

import html
import io
import re
from html.parser import HTMLParser
from typing import Any, Callable, Optional

from .. import config
from . import ocr
from .model import Block, BlockKind, ExtractedDocument, ExtractedPage, PageStatus
from .validation import DOCX, JPEG, PDF, PNG, PPTX, TIFF


def _version(mod_name: str) -> str:
    try:
        from importlib.metadata import version
        return version(mod_name)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- PDF

_PRINTED_LABEL_RES = [
    # "VII | P a g e", "11 | P a g e", "Page 12", "12 | Page"
    re.compile(r"^\s*([ivxlcdmIVXLCDM]{1,7}|\d{1,4})\s*\|\s*P\s?a\s?g\s?e\b"),
    re.compile(r"^\s*P\s?a\s?g\s?e\s+([ivxlcdmIVXLCDM]{1,7}|\d{1,4})\b", re.I),
    re.compile(r"^\s*-\s*(\d{1,4})\s*-\s*$"),
]


def _printed_label(text: str) -> Optional[str]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines[:3] + lines[-3:]:
        for rx in _PRINTED_LABEL_RES:
            m = rx.match(ln)
            if m:
                return m.group(1)
    return None


def _garbled(text: str) -> bool:
    """Text layer present but unusable (missing font maps, CID garbage)."""
    if not text:
        return False
    bad = text.count("�") + len(re.findall(r"\(cid:\d+\)", text)) * 5
    bad += sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    return bad / max(len(text), 1) > 0.15


def _ocr_pdf_page(pdf_doc, index: int) -> tuple[str, float]:
    page = pdf_doc[index]
    image = page.render(scale=config.OCR_DPI / 72).to_pil()
    try:
        return ocr.ocr_image(image)
    finally:
        page.close()


def extract_pdf(data: bytes) -> ExtractedDocument:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    n = len(reader.pages)
    if n > config.MAX_PAGES:
        raise ValueError(f"{n} pages exceeds limit {config.MAX_PAGES}")
    try:
        declared_labels = list(reader.page_labels)
    except Exception:
        declared_labels = []
    ocr_on = ocr.available()
    pdfium_doc = None
    pages: list[ExtractedPage] = []
    for i in range(n):
        pg = ExtractedPage(number=i + 1, basis="physical")
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as exc:
            text = ""
            pg.warnings.append(f"native extraction error: {type(exc).__name__}")
        stripped = text.strip()
        needs_ocr = len(stripped) < config.OCR_MIN_CHARS or _garbled(stripped)
        if needs_ocr and ocr_on:
            try:
                if pdfium_doc is None:
                    import pypdfium2
                    pdfium_doc = pypdfium2.PdfDocument(data)
                o_text, conf = _ocr_pdf_page(pdfium_doc, i)
                if len(o_text.strip()) > len(stripped) and not (len(stripped) >= 20 and conf < 30):
                    pg.raw_text, pg.method, pg.ocr_confidence = o_text, "ocr", conf
                    pg.status = PageStatus.OCR if conf >= config.OCR_LOW_CONFIDENCE else PageStatus.LOW_CONFIDENCE
                else:
                    pg.raw_text = text
                    pg.status = PageStatus.OK if stripped else PageStatus.EMPTY
                    if not stripped:
                        pg.method = "none"
            except Exception as exc:
                pg.raw_text = text
                pg.status = PageStatus.FAILED if not stripped else PageStatus.OK
                pg.error = f"ocr failed: {type(exc).__name__}: {str(exc)[:160]}"
        else:
            pg.raw_text = text
            if not stripped:
                pg.status, pg.method = PageStatus.EMPTY, "none"
                if not ocr_on:
                    pg.warnings.append("no text layer and OCR unavailable")
            elif _garbled(stripped):
                pg.status = PageStatus.LOW_CONFIDENCE
                pg.warnings.append("garbled text layer and OCR unavailable")
        declared = declared_labels[i] if i < len(declared_labels) else None
        printed = _printed_label(pg.raw_text)
        # Prefer the label printed on the page; the PDF's declared labels are
        # often just 1..n when the producer never set real labels.
        pg.label = printed or (declared if declared and declared != str(i + 1) else None)
        pages.append(pg)
    if pdfium_doc is not None:
        pdfium_doc.close()

    meta = reader.metadata or {}
    props = {
        "title": str(meta.get("/Title") or "") or None,
        "producer": str(meta.get("/Producer") or "") or None,
        "created": str(meta.get("/CreationDate") or "") or None,
    }
    return ExtractedDocument(
        mime_type=PDF, extractor="pdf/pypdf", extractor_version=f"pypdf {_version('pypdf')}",
        pages=pages, ocr=ocr.engine_info() if any(p.method == "ocr" for p in pages) else None,
        doc_properties={k: v for k, v in props.items() if v},
    )


# ---------------------------------------------------------------- DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"


_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _iter_skip_fallback(el):
    """Document-order walk that skips mc:Fallback subtrees. Word stores text
    boxes twice (a DrawingML choice and a legacy VML fallback); reading both
    doubles every text-box caption."""
    yield el
    for child in el:
        if child.tag == _MC_FALLBACK:
            continue
        yield from _iter_skip_fallback(child)


def _para_text(p_el) -> tuple[str, int, bool]:
    """Text of a w:p in document order including OMML maths, the number of page
    breaks inside it, and whether it holds an equation."""
    out: list[str] = []
    breaks = 0
    has_math = False
    for el in _iter_skip_fallback(p_el):
        tag = el.tag
        if tag == _W + "t" or tag == _M + "t":
            out.append(el.text or "")
            if tag == _M + "t":
                has_math = True
        elif tag == _W + "tab":
            out.append("\t")
        elif tag == _W + "br":
            if el.get(_W + "type") == "page":
                breaks += 1
            else:
                out.append("\n")
        elif tag == _W + "lastRenderedPageBreak":
            breaks += 1
    return "".join(out), breaks, has_math


def _heading_level(style_name: str) -> int:
    s = (style_name or "").lower()
    if s == "title":
        return 1
    m = re.match(r"heading\s*(\d)", s)
    return int(m.group(1)) if m else 0


def extract_docx(data: bytes) -> ExtractedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    body = d.element.body
    has_rendered_breaks = body.find(".//" + _W + "lastRenderedPageBreak") is not None \
        or body.find(".//" + _W + "br[@" + _W + "type='page']") is not None
    basis = "rendered_estimate" if has_rendered_breaks else "none"

    pages: dict[int, ExtractedPage] = {}
    page_no = 1

    def page(n: int) -> ExtractedPage:
        if n not in pages:
            pages[n] = ExtractedPage(number=n, basis=basis)
        return pages[n]

    p_idx = t_idx = 0
    for child in body.iterchildren():
        if child.tag == _W + "p":
            p_idx += 1
            para = Paragraph(child, d)
            text, breaks, has_math = _para_text(child)
            # A break at the very start belongs to the page this paragraph is on.
            starts_with_break = False
            for el in child.iter():
                if el.tag in (_W + "t", _M + "t") and (el.text or "").strip():
                    break
                if el.tag == _W + "lastRenderedPageBreak" or (
                        el.tag == _W + "br" and el.get(_W + "type") == "page"):
                    starts_with_break = True
                    break
            if starts_with_break and has_rendered_breaks:
                page_no += 1
                breaks -= 1
            stripped = text.strip()
            if stripped:
                style = para.style.name if para.style is not None else ""
                lvl = _heading_level(style)
                is_list = "list" in style.lower() or child.find(".//" + _W + "numPr") is not None
                if lvl:
                    blk = Block(BlockKind.HEADING, stripped, level=lvl, anchor=f"p{p_idx}")
                elif has_math and len(re.sub(r"[\s\d=+\-*/^().,]", "", stripped)) < len(stripped) * 0.6:
                    blk = Block(BlockKind.EQUATION, stripped, anchor=f"p{p_idx}")
                elif is_list:
                    blk = Block(BlockKind.LIST_ITEM, stripped, level=1, anchor=f"p{p_idx}")
                else:
                    blk = Block(BlockKind.PARAGRAPH, stripped, anchor=f"p{p_idx}")
                page(page_no).blocks.append(blk)
            for drawing in child.iter(_WP + "docPr"):
                alt = (drawing.get("descr") or "").strip()
                if alt and len(alt) > 12 and not alt.lower().startswith(("a picture containing", "image")):
                    page(page_no).blocks.append(Block(BlockKind.CAPTION, f"Figure: {alt}", anchor=f"p{p_idx}"))
            if has_rendered_breaks:
                page_no += max(breaks, 0)
        elif child.tag == _W + "tbl":
            t_idx += 1
            tbl = Table(child, d)
            rows: list[list[str]] = []
            for r in tbl.rows:
                cells: list[str] = []
                prev = None
                for c in r.cells:
                    if prev is not None and c._tc is prev:
                        continue  # horizontally merged cell repeats
                    prev = c._tc
                    cells.append(re.sub(r"\s+", " ", c.text).strip())
                if any(cells):
                    rows.append(cells)
            if rows:
                text = "\n".join(" | ".join(r) for r in rows)
                page(page_no).blocks.append(Block(BlockKind.TABLE, text, rows=rows, anchor=f"t{t_idx}"))
            if has_rendered_breaks:
                page_no += len(child.findall(".//" + _W + "lastRenderedPageBreak"))

    ordered = [pages[k] for k in sorted(pages)] or [ExtractedPage(number=1, basis=basis)]
    for pg in ordered:
        if not pg.blocks:
            pg.status, pg.method = PageStatus.EMPTY, "none"
    cp = d.core_properties
    props = {"title": cp.title or None, "revision": cp.revision,
             "modified": cp.modified.isoformat() if cp.modified else None}
    return ExtractedDocument(
        mime_type=DOCX, extractor="docx/python-docx", extractor_version=f"python-docx {_version('python-docx')}",
        pages=ordered, doc_properties={k: v for k, v in props.items() if v},
        warnings=[] if has_rendered_breaks else ["no rendered page breaks; page numbers unavailable"],
    )


# ---------------------------------------------------------------- PPTX

def extract_pptx(data: bytes) -> ExtractedDocument:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    prs = Presentation(io.BytesIO(data))
    pages: list[ExtractedPage] = []

    def walk(shapes, out: list):
        for sh in shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(sh.shapes, out)
            else:
                out.append(sh)

    for s_idx, slide in enumerate(prs.slides, start=1):
        pg = ExtractedPage(number=s_idx, basis="slide", label=str(s_idx))
        shapes: list = []
        walk(slide.shapes, shapes)
        shapes.sort(key=lambda s: ((s.top or 0) // 20000, s.left or 0))
        for sh in shapes:
            anchor = f"s{s_idx}.{sh.shape_id}"
            is_title = False
            if sh.is_placeholder:
                try:
                    is_title = sh.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                except Exception:
                    is_title = False
            if sh.has_text_frame:
                if is_title:
                    t = sh.text_frame.text.strip()
                    if t:
                        pg.blocks.append(Block(BlockKind.HEADING, re.sub(r"\s+", " ", t), level=1, anchor=anchor))
                    continue
                for para in sh.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if not t:
                        continue
                    if para.level > 0 or len(sh.text_frame.paragraphs) > 1:
                        pg.blocks.append(Block(BlockKind.LIST_ITEM, t, level=para.level + 1, anchor=anchor))
                    else:
                        pg.blocks.append(Block(BlockKind.PARAGRAPH, t, anchor=anchor))
            elif getattr(sh, "has_table", False) and sh.has_table:
                rows = [[re.sub(r"\s+", " ", c.text).strip() for c in r.cells] for r in sh.table.rows]
                rows = [r for r in rows if any(r)]
                if rows:
                    pg.blocks.append(Block(BlockKind.TABLE, "\n".join(" | ".join(r) for r in rows),
                                           rows=rows, anchor=anchor))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                pg.blocks.append(Block(BlockKind.NOTES, notes, anchor=f"s{s_idx}.notes"))
        if not pg.blocks:
            pg.status, pg.method = PageStatus.EMPTY, "none"
        pages.append(pg)
    return ExtractedDocument(
        mime_type=PPTX, extractor="pptx/python-pptx", extractor_version=f"python-pptx {_version('python-pptx')}",
        pages=pages or [ExtractedPage(number=1, basis="slide", status=PageStatus.EMPTY, method="none")],
        doc_properties={"title": prs.core_properties.title} if prs.core_properties.title else {},
    )


# ---------------------------------------------------------------- images

def extract_image(data: bytes, mime: str) -> ExtractedDocument:
    from PIL import Image, ImageSequence

    pages: list[ExtractedPage] = []
    if not ocr.available():
        return ExtractedDocument(
            mime_type=mime, extractor="image/tesseract", extractor_version="unavailable",
            pages=[ExtractedPage(number=1, status=PageStatus.FAILED, method="none",
                                 error="OCR engine unavailable for an image-only source")],
        )
    with Image.open(io.BytesIO(data)) as im:
        frames = [f.copy() for f in ImageSequence.Iterator(im)][: config.MAX_PAGES]
    for i, frame in enumerate(frames, start=1):
        pg = ExtractedPage(number=i, method="ocr")
        try:
            text, conf = ocr.ocr_image(frame)
            pg.raw_text, pg.ocr_confidence = text, conf
            if not text.strip():
                pg.status, pg.method = PageStatus.EMPTY, "none"
            else:
                pg.status = PageStatus.OCR if conf >= config.OCR_LOW_CONFIDENCE else PageStatus.LOW_CONFIDENCE
        except Exception as exc:
            pg.status, pg.error = PageStatus.FAILED, f"ocr failed: {type(exc).__name__}"
        pages.append(pg)
    info = ocr.engine_info() or {}
    return ExtractedDocument(mime_type=mime, extractor="image/tesseract",
                             extractor_version=f"tesseract {info.get('version', '?')}", pages=pages, ocr=info)


# ---------------------------------------------------------------- LMS / EduNeX HTML

class _HtmlBlocks(HTMLParser):
    BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "figcaption", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._buf: list[str] = []
        self._tag: Optional[str] = None
        self._skip = 0
        self._table: Optional[list[list[str]]] = None
        self._row: Optional[list[str]] = None
        self._cell: Optional[list[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "iframe"):
            self._skip += 1
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in self.BLOCK_TAGS and self._cell is None:
            self._flush()
            self._tag = tag
        elif tag == "br":
            (self._cell if self._cell is not None else self._buf).append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "iframe"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.blocks.append(Block(BlockKind.TABLE, "\n".join(" | ".join(r) for r in self._table),
                                         rows=self._table, anchor=f"t{len(self.blocks)}"))
            self._table = None
        elif tag in self.BLOCK_TAGS and self._cell is None:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        (self._cell if self._cell is not None else self._buf).append(data)

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        tag = self._tag or "p"
        self._buf, self._tag = [], None
        if not text:
            return
        anchor = f"h{len(self.blocks)}"
        if tag[0] == "h" and tag[1:].isdigit():
            self.blocks.append(Block(BlockKind.HEADING, text, level=int(tag[1]), anchor=anchor))
        elif tag == "li":
            self.blocks.append(Block(BlockKind.LIST_ITEM, text, level=1, anchor=anchor))
        elif tag == "figcaption":
            self.blocks.append(Block(BlockKind.CAPTION, text, anchor=anchor))
        else:
            self.blocks.append(Block(BlockKind.PARAGRAPH, text, anchor=anchor))

    def close(self):
        super().close()
        self._flush()


def extract_lms_html(body: str, is_html: bool = True) -> ExtractedDocument:
    pg = ExtractedPage(number=1, basis="none")
    if is_html:
        parser = _HtmlBlocks()
        parser.feed(body)
        parser.close()
        pg.blocks = parser.blocks
    else:
        pg.raw_text = html.unescape(body)
    if not pg.blocks and not pg.raw_text.strip():
        pg.status, pg.method = PageStatus.EMPTY, "none"
    return ExtractedDocument(mime_type="text/html" if is_html else "text/plain", extractor="lms/html",
                             extractor_version="nexa-html 1.0", pages=[pg])


EXTRACTORS: dict[str, Callable[[bytes], ExtractedDocument]] = {
    PDF: extract_pdf,
    DOCX: extract_docx,
    PPTX: extract_pptx,
    PNG: lambda b: extract_image(b, PNG),
    JPEG: lambda b: extract_image(b, JPEG),
    TIFF: lambda b: extract_image(b, TIFF),
}


def extract(mime: str, data: bytes) -> ExtractedDocument:
    fn = EXTRACTORS.get(mime)
    if fn is None:
        raise ValueError(f"no extractor for {mime}")
    return fn(data)


def versions() -> dict[str, Any]:
    return {
        "pypdf": _version("pypdf"),
        "pypdfium2": _version("pypdfium2"),
        "python-docx": _version("python-docx"),
        "python-pptx": _version("python-pptx"),
        "ocr": ocr.engine_info(),
    }
