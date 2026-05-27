from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Dict, List

"""Text cleanup and structure detection helpers for OCR and document extraction.

This module normalizes noisy text, removes common extraction artifacts, and
reassembles raw lines into logical blocks such as headings, paragraphs, list
items, and table rows.
"""


def normalize_text_basic(text: str) -> str:
    """Apply lightweight cleanup for line-level comparisons and filtering."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return " ".join(text.split()).strip()


def remove_misc_symbols(text: str) -> str:
    """Remove decorative symbols that usually hurt embedding quality."""
    text = re.sub(r"[■□▪▫◆◇○●◦※¤§¶©®™]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text


def is_noise_line(line: str) -> bool:
    """Detect lines that should not become part of content blocks."""
    compact = normalize_text_basic(line)
    if not compact:
        return True

    watermark_or_legal = [
        r"all rights reserved",
        r"copyright\s*(?:\(c\)|©)?",
        r"do not (?:copy|distribute|reproduce)",
        r"confidential",
        r"draft",
        r"sample watermark",
        r"for internal use only",
        r"unauthorized",
    ]
    if any(re.search(pattern, compact, flags=re.IGNORECASE) for pattern in watermark_or_legal):
        return True

    if re.fullmatch(r"(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?", compact, flags=re.IGNORECASE):
        return True

    if re.fullmatch(r"[^A-Za-z0-9]{2,}", compact):
        return True

    return False


def remove_noise_lines(text: str) -> str:
    """Drop noisy lines like page markers, legal boilerplate, and symbols."""
    lines = text.splitlines()
    cleaned_lines = [line for line in lines if not is_noise_line(line)]
    return "\n".join(cleaned_lines)


def normalize_text(text: str) -> str:
    """Perform full semantic denoising on extracted text."""
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    replacements = {
        "\u00a0": " ",
        "\ufeff": "",
        "â€™": "'",
        "â€˜": "'",
        "â€œ": '"',
        "â€\x9d": '"',
        "â€“": "-",
        "â€”": "-",
        "â€¦": "...",
        "Â": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"([A-Za-z])-[ \t]*\n[ \t]*([A-Za-z])", r"\1\2", text)
    text = re.sub(r"(?<![.!?:;])\n(?!\n)", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    text = remove_noise_lines(text)
    text = remove_misc_symbols(text)
    return " ".join(text.split()).strip()


def detect_running_header_footer_lines(page_lines: List[List[str]]) -> Dict[str, set]:
    """Find repeated top and bottom lines that likely act as headers or footers."""
    header_counter: Counter[str] = Counter()
    footer_counter: Counter[str] = Counter()

    for lines in page_lines:
        normalized = [normalize_text_basic(line) for line in lines if normalize_text_basic(line)]
        if not normalized:
            continue

        for candidate in normalized[:2]:
            header_counter[candidate] += 1
        for candidate in normalized[-2:]:
            footer_counter[candidate] += 1

    min_repeats = 2
    headers = {line for line, count in header_counter.items() if count >= min_repeats}
    footers = {line for line, count in footer_counter.items() if count >= min_repeats}
    return {"headers": headers, "footers": footers}


def remove_running_header_footer(lines: List[str], repeated: Dict[str, set]) -> List[str]:
    """Remove repeated headers and footers from a page's extracted lines."""
    headers = repeated.get("headers", set())
    footers = repeated.get("footers", set())

    filtered: List[str] = []
    for index, line in enumerate(lines):
        compact = normalize_text_basic(line)
        if not compact:
            continue

        if index <= 1 and compact in headers:
            continue
        if index >= max(0, len(lines) - 2) and compact in footers:
            continue
        if is_noise_line(compact):
            continue
        filtered.append(line)

    return filtered


def is_heading_line(text: str) -> bool:
    """Heuristically identify heading-like lines."""
    if not text:
        return False

    stripped = text.strip()
    if len(stripped) > 90:
        return False

    words = stripped.split()
    if not words:
        return False

    if stripped.isupper() and len(words) <= 12:
        return True

    if stripped.endswith((".", ":", ";")):
        return False

    alpha_words = sum(1 for word in words if any(char.isalpha() for char in word))
    title_case_words = sum(1 for word in words if word[:1].isupper())
    return alpha_words >= 2 and title_case_words >= max(2, len(words) // 2) and len(words) <= 10


def is_list_item_line(text: str) -> bool:
    """Detect list item prefixes such as bullets and numbered items."""
    stripped = text.strip()
    return bool(
        stripped
        and (
            stripped.startswith(("- ", "* ", "• "))
            or bool(re.match(r"^(\d+|[a-zA-Z])[\.)]\s+", stripped))
        )
    )


def is_table_row_line(text: str) -> bool:
    """Detect lines that look like table rows based on separators and spacing."""
    stripped = text.strip()
    if not stripped:
        return False

    pipe_cells = stripped.count("|") >= 1
    tab_cells = "\t" in stripped
    spaced_cells = len(re.split(r"\s{2,}", stripped)) >= 3
    return pipe_cells or tab_cells or spaced_cells


def line_to_structured_block(text: str, base_metadata: Dict) -> Dict:
    """Tag a single line with the structural element type it most likely is."""
    if is_heading_line(text):
        element_type = "heading"
    elif is_list_item_line(text):
        element_type = "list_item"
    elif is_table_row_line(text):
        element_type = "table_row"
    else:
        element_type = "paragraph"

    return {
        "text": normalize_text(text),
        "metadata": {
            **base_metadata,
            "element_type": element_type,
        },
    }


def reassemble_structured_blocks(lines: List[str], base_metadata: Dict) -> List[Dict]:
    """Group raw lines into coherent blocks before chunking."""
    blocks: List[Dict] = []
    paragraph_buffer: List[str] = []
    table_buffer: List[str] = []

    def flush_paragraph() -> None:
        if paragraph_buffer:
            blocks.append(
                {
                    "text": normalize_text(" ".join(paragraph_buffer)),
                    "metadata": {**base_metadata, "element_type": "paragraph"},
                }
            )
            paragraph_buffer.clear()

    def flush_table() -> None:
        if table_buffer:
            blocks.append(
                {
                    "text": "\n".join(table_buffer).strip(),
                    "metadata": {**base_metadata, "element_type": "table"},
                }
            )
            table_buffer.clear()

    for raw_line in lines:
        line = normalize_text(raw_line)
        if not line:
            flush_paragraph()
            flush_table()
            continue

        if is_heading_line(line):
            flush_paragraph()
            flush_table()
            blocks.append(line_to_structured_block(line, base_metadata))
            continue

        if is_list_item_line(line):
            flush_paragraph()
            flush_table()
            blocks.append(line_to_structured_block(line, base_metadata))
            continue

        if is_table_row_line(line):
            flush_paragraph()
            table_buffer.append(line)
            continue

        flush_table()
        paragraph_buffer.append(line)

    flush_paragraph()
    flush_table()
    return blocks
