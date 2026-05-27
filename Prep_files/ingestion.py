from __future__ import annotations

import importlib
import unicodedata
import re
from pathlib import Path
from typing import Dict, List

from text_processing import (
    detect_running_header_footer_lines,
    is_noise_line,
    normalize_text,
    remove_running_header_footer,
    reassemble_structured_blocks,
)

"""File ingestion helpers for PDFs, PPTX files, and images.

Each parser extracts raw text differently, then passes the output into the
shared structural parsing helpers in text_processing.py.
"""


def ingest_image_file(file_path: str) -> List[Dict]:
    """Use OCR to extract text blocks from image files."""
    easyocr_module = importlib.import_module("easyocr")
    reader = easyocr_module.Reader(["en"], gpu=False)
    raw_blocks = reader.readtext(file_path, detail=0)

    blocks: List[Dict] = []
    for line_number, text in enumerate(raw_blocks, start=1):
        normalized_text = normalize_text(text)
        if not normalized_text:
            continue

        blocks.append(
            {
                "text": normalized_text,
                "metadata": {
                    "source_file": file_path,
                    "source_type": "image",
                    "extraction_method": "easyocr",
                    "line_number": line_number,
                },
            }
        )

    return blocks


def ingest_pdf_file(file_path: str) -> List[Dict]:
    """Extract PDF text directly first, then OCR pages that still need recovery."""
    pypdf_module = importlib.import_module("pypdf")
    pdf2image_module = importlib.import_module("pdf2image")
    easyocr_module = importlib.import_module("easyocr")

    PdfReader = getattr(pypdf_module, "PdfReader")
    convert_from_path = getattr(pdf2image_module, "convert_from_path")
    reader = easyocr_module.Reader(["en"], gpu=False)

    blocks: List[Dict] = []
    extracted_page_lines: List[List[str]] = []
    with open(file_path, "rb") as pdf_stream:
        pdf_reader = PdfReader(pdf_stream)

        for page in pdf_reader.pages:
            page_text = page.extract_text() or ""
            page_text = unicodedata.normalize("NFKC", page_text)
            page_text = re.sub(r"([A-Za-z])-[ \t]*\n[ \t]*([A-Za-z])", r"\1\2", page_text)
            extracted_page_lines.append([line for line in page_text.splitlines() if line.strip()])

        repeated = detect_running_header_footer_lines(extracted_page_lines)

        for page_number, page in enumerate(pdf_reader.pages, start=1):
            page_text = page.extract_text() or ""
            page_text = unicodedata.normalize("NFKC", page_text)
            page_text = re.sub(r"([A-Za-z])-[ \t]*\n[ \t]*([A-Za-z])", r"\1\2", page_text)
            raw_page_lines = [line for line in page_text.splitlines() if line.strip()]
            page_lines = remove_running_header_footer(raw_page_lines, repeated)

            if page_lines:
                blocks.extend(
                    reassemble_structured_blocks(
                        page_lines,
                        {
                            "source_file": file_path,
                            "source_type": "pdf",
                            "extraction_method": "pypdf",
                            "page_number": page_number,
                        },
                    )
                )
                continue

            if normalize_text(page_text):
                blocks.append(
                    {
                        "text": normalize_text(page_text),
                        "metadata": {
                            "source_file": file_path,
                            "source_type": "pdf",
                            "extraction_method": "pypdf",
                            "page_number": page_number,
                        },
                    }
                )
                continue

            page_images = convert_from_path(
                file_path,
                first_page=page_number,
                last_page=page_number,
            )
            for image_index, page_image in enumerate(page_images, start=1):
                ocr_blocks = reader.readtext(page_image, detail=0)
                structured_blocks = reassemble_structured_blocks(
                    [line for line in ocr_blocks if not is_noise_line(line)],
                    {
                        "source_file": file_path,
                        "source_type": "pdf",
                        "extraction_method": "easyocr",
                        "page_number": page_number,
                        "page_image_index": image_index,
                    },
                )
                for block in structured_blocks:
                    block["metadata"]["ocr_line_count"] = len(ocr_blocks)
                blocks.extend(structured_blocks)

    return blocks


def ingest_pptx_file(file_path: str) -> List[Dict]:
    """Extract slide and shape text from PowerPoint presentations."""
    pptx_module = importlib.import_module("pptx")
    Presentation = getattr(pptx_module, "Presentation")

    blocks: List[Dict] = []
    with open(file_path, "rb") as pptx_stream:
        presentation = Presentation(pptx_stream)

        for slide_number, slide in enumerate(presentation.slides, start=1):
            for shape_index, shape in enumerate(slide.shapes, start=1):
                if not getattr(shape, "has_text_frame", False):
                    continue

                text_frame = getattr(shape, "text_frame", None)
                if text_frame is None:
                    continue

                shape_lines: List[str] = []
                for paragraph in text_frame.paragraphs:
                    paragraph_text = normalize_text("".join(run.text for run in paragraph.runs))
                    if not paragraph_text:
                        continue
                    if is_noise_line(paragraph_text):
                        continue

                    prefix = ""
                    if paragraph.level > 0 or getattr(paragraph, "bullet", None) is not None:
                        prefix = "- "
                    shape_lines.append(f"{prefix}{paragraph_text}")

                if not shape_lines:
                    shape_text = normalize_text(getattr(shape, "text", ""))
                    if not shape_text:
                        continue
                    if is_noise_line(shape_text):
                        continue
                    shape_lines = [shape_text]

                blocks.extend(
                    reassemble_structured_blocks(
                        shape_lines,
                        {
                            "source_file": file_path,
                            "source_type": "pptx",
                            "extraction_method": "python-pptx",
                            "slide_number": slide_number,
                            "shape_index": shape_index,
                        },
                    )
                )

    return blocks


def ingest_file(file_path: str) -> List[Dict]:
    """Route a file to the correct ingestion strategy based on its extension."""
    suffix = Path(file_path).suffix.lower()
    if suffix == ".pdf":
        return ingest_pdf_file(file_path)
    if suffix == ".pptx":
        return ingest_pptx_file(file_path)
    return ingest_image_file(file_path)
