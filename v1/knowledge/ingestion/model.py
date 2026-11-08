"""Internal document model produced by extractors (NEXA-M2-002).

Every extractor returns an ExtractedDocument: a list of pages, each with
structured Blocks where the format gives us structure (DOCX, PPTX, HTML) or raw
text lines where it does not (PDF, OCR). Normalization and the structurer turn
raw lines into Blocks before chunking (NEXA-M2-003).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    EQUATION = "equation"
    EXERCISE = "exercise"      # a question or task prompt
    CAPTION = "caption"
    NOTES = "notes"            # speaker notes on a slide


class PageStatus(str, Enum):
    OK = "ok"
    OCR = "ocr"                       # text came from OCR at acceptable confidence
    LOW_CONFIDENCE = "low_confidence" # OCR below threshold, kept but flagged
    EMPTY = "empty"                   # no text found (diagram-only or blank page)
    FAILED = "failed"                 # extraction raised


@dataclass
class Block:
    kind: BlockKind
    text: str
    level: int = 0                       # heading level, list depth
    rows: Optional[list[list[str]]] = None  # tables: header row first
    anchor: Optional[str] = None         # stable in-document anchor (p12, t3, s4)
    confidence: Optional[float] = None   # OCR word confidence 0..100
    meta: Optional[dict[str, Any]] = None  # e.g. recognised curriculum heading fields

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class ExtractedPage:
    number: int                           # 1-based physical page or slide number
    status: PageStatus = PageStatus.OK
    method: str = "native"                # native | ocr | none
    label: Optional[str] = None           # printed page label when known
    basis: str = "physical"               # physical | rendered_estimate | slide | none
    raw_text: str = ""                    # unstructured text (PDF, OCR)
    blocks: list[Block] = field(default_factory=list)  # structured blocks (DOCX, PPTX, HTML)
    ocr_confidence: Optional[float] = None
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def char_count(self) -> int:
        return len(self.raw_text) + sum(len(b.text) for b in self.blocks)


@dataclass
class ExtractedDocument:
    mime_type: str
    extractor: str                         # e.g. pdf/pypdf
    extractor_version: str                 # library version, recorded for reproducibility
    pages: list[ExtractedPage]
    ocr: Optional[dict[str, Any]] = None   # engine, version, language, dpi, psm, oem
    doc_properties: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        by: dict[str, int] = {}
        for p in self.pages:
            by[p.status.value] = by.get(p.status.value, 0) + 1
        return {
            "pages": len(self.pages),
            "by_status": by,
            "chars": sum(p.char_count for p in self.pages),
            "low_confidence_pages": [p.number for p in self.pages if p.status == PageStatus.LOW_CONFIDENCE],
            "failed_pages": [p.number for p in self.pages if p.status == PageStatus.FAILED],
            "empty_pages": [p.number for p in self.pages if p.status == PageStatus.EMPTY],
            "ocr_pages": [p.number for p in self.pages if p.method == "ocr"],
        }

    @property
    def overall_status(self) -> str:
        """succeeded | partial | failed. A document with no usable text fails."""
        usable = [p for p in self.pages if p.status in (PageStatus.OK, PageStatus.OCR, PageStatus.LOW_CONFIDENCE)]
        failed = [p for p in self.pages if p.status == PageStatus.FAILED]
        if not usable:
            return "failed"
        if failed or any(p.status == PageStatus.LOW_CONFIDENCE for p in self.pages):
            return "partial"
        return "succeeded"
