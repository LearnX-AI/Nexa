"""Content normalization (NEXA-M2-003), version NORMALIZER_VERSION.

Goal: remove extraction noise without changing educational meaning.

Deliberately NOT done:
- No NFKC. NFKC folds H₂O to H2O, x² to x2, µ to μ and the prime in 5′ to an
  apostrophe. We use NFC plus an explicit, reviewed map of safe replacements.
- No spelling correction or case changes.
- No removal of numbers, symbols or units.

Done, in order:
1. Unicode NFC, ligature and invisible-character repair, common mojibake repair.
2. Letter-spaced line repair ("G r a d e  1 2" -> "Grade 12").
3. Document-level removal of repeated running headers and footers, and of
   printed page-number lines (the label is kept on the page as provenance).
4. Table-of-contents dot leaders removed.
5. Worksheet answer lines collapsed to a single "[answer space]" marker.
6. OCR noise lines dropped; de-hyphenation of words broken across lines.
7. Whitespace collapsed.

Every change is counted so the effect of a normalizer version is measurable.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

NORMALIZER_VERSION = "1.0.0"

_SAFE_MAP = {
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    " ": " ", " ": " ", " ": " ", " ": " ", "　": " ",
    "­": "", "​": "", "‌": "", "‍": "", "﻿": "", "⁠": "",
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "−": "−",  # keep the real minus sign
    "": "•", "": "•", "": "•", "": "•", "": "•",  # Symbol-font bullets
}
_MOJIBAKE = {
    "â€™": "'", "â€˜": "'", "â€œ": '"', "â€\u009d": '"',
    "â€“": "–", "â€”": "—", "â€¦": "…",
    "Â°": "°", "Âµ": "µ", "Ã©": "é",
}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BULLET_START = re.compile(r"^\s*[•▪●◦■□‣⁃➢✓✔·]\s*")
_TOC_LINE = re.compile(r"(?:\.\s?){6,}\s*[ivxlcdmIVXLCDM\d]+\s*$")
_ANSWER_LINE = re.compile(r"^[\s_.]*_{5,}[\s_.]*$")
_INLINE_UNDERSCORES = re.compile(r"_{5,}")
_PAGE_NUM_LINE = re.compile(
    r"^\s*(?:(?:[ivxlcdmIVXLCDM]{1,7}|\d{1,4})\s*\|\s*P\s?a\s?g\s?e|P\s?a\s?g\s?e\s+\d{1,4}(?:\s*of\s*\d+)?|"
    r"-\s*\d{1,4}\s*-|\d{1,4})\s*$", re.I)
_KEEP_HYPHEN_PREFIXES = {"self", "non", "co", "anti", "semi", "multi", "well", "cross", "high", "low",
                         "long", "short", "two", "three", "one", "half", "pre", "post", "sub", "inter"}


@dataclass
class NormStats:
    counts: Counter = field(default_factory=Counter)

    def add(self, key: str, n: int = 1) -> None:
        if n:
            self.counts[key] += n


def normalize_unicode(text: str, stats: NormStats | None = None) -> str:
    stats = stats or NormStats()
    for bad, good in _MOJIBAKE.items():
        if bad in text:
            stats.add("mojibake_fixed", text.count(bad))
            text = text.replace(bad, good)
    text = unicodedata.normalize("NFC", text)
    out = []
    for ch in text:
        rep = _SAFE_MAP.get(ch)
        if rep is not None and rep != ch:
            stats.add("unicode_mapped")
            out.append(rep)
        else:
            out.append(ch)
    text = "".join(out)
    n = len(_CTRL.findall(text))
    stats.add("control_chars_removed", n)
    return _CTRL.sub("", text)


def _fix_letter_spacing(line: str, stats: NormStats) -> str:
    tokens = line.split()
    if len(tokens) < 6:
        return line
    singles = sum(1 for t in tokens if len(t) == 1)
    if singles / len(tokens) < 0.75:
        return line
    words = [w.replace(" ", "") for w in re.split(r"\s{2,}", line.strip())]
    stats.add("letter_spacing_fixed")
    return " ".join(w for w in words if w)


def _line_key(line: str) -> str:
    s = line.strip().lower()
    s = re.sub(r"\b[ivxlcdm]{1,7}\b", "#", s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s)


def repeated_edge_lines(pages: list[list[str]], edge: int = 3) -> set[str]:
    """Keys of lines that repeat at the top or bottom of many pages."""
    counter: Counter = Counter()
    for lines in pages:
        content = [ln for ln in lines if ln.strip()]
        # Running headers and footers are short and never stop mid-word or
        # mid-list, so long lines and continuation lines are never candidates.
        keys = {_line_key(ln) for ln in content[:edge] + content[-edge:]
                if len(ln.strip()) <= 80 and not re.search(r"[-,;]\s*$", ln) and not _ANSWER_LINE.match(ln)
                and sum(ch.isalnum() for ch in ln) >= 3}
        counter.update(k for k in keys if k and len(k) > 1)
    n = len(pages)
    threshold = max(3, int(0.4 * n + 0.5))
    if n < 3:
        return set()
    return {k for k, c in counter.items() if c >= threshold and len(k) <= 120}


def _dehyphenate(lines: list[str], stats: NormStats) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if i + 1 < len(lines):
            nxt = lines[i + 1].lstrip()
            m = re.search(r"([A-Za-z]+)-\s*$", ln)
            if m and nxt and nxt[0].islower():
                frag = m.group(1).lower()
                stats.add("dehyphenated")
                if frag in _KEEP_HYPHEN_PREFIXES:
                    joined = ln.rstrip()[:-1].rstrip() + "-"
                else:
                    joined = ln.rstrip()[:-1].rstrip()
                first, _, rest = nxt.partition(" ")
                out.append(joined + first)
                lines[i + 1] = rest
                i += 1
                continue
        out.append(ln)
        i += 1
    return out


def _is_ocr_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    alnum = sum(ch.isalnum() for ch in s)
    if len(s) <= 3 and alnum <= 1 and not re.match(r"^[A-Za-z0-9][.)]?$", s):
        return True
    return len(s) >= 4 and alnum / len(s) < 0.25 and not re.search(r"[=+→⇌<>±]", s)


def normalize_pages(page_texts: list[str], is_ocr: list[bool] | None = None) -> tuple[list[str], NormStats]:
    """Normalize raw page texts of one document. Returns texts in the same order."""
    stats = NormStats()
    is_ocr = is_ocr or [False] * len(page_texts)
    split: list[list[str]] = []
    for t in page_texts:
        t = normalize_unicode(t, stats)
        lines = [_fix_letter_spacing(ln.rstrip(), stats) for ln in t.splitlines()]
        split.append(lines)

    running = repeated_edge_lines(split)
    result: list[str] = []
    for lines, ocr_page in zip(split, is_ocr):
        kept: list[str] = []
        content = [i for i, ln in enumerate(lines) if ln.strip()]
        edges = set(content[:3] + content[-3:])
        answer_run = False
        for i, ln in enumerate(lines):
            s = ln.strip()
            if _ANSWER_LINE.match(s):
                if not answer_run:
                    kept.append("[answer space]")
                    stats.add("answer_lines_collapsed")
                answer_run = True
                continue
            if i in edges and _PAGE_NUM_LINE.match(s):
                stats.add("page_number_lines_removed")
                continue
            if i in edges and _line_key(s) in running:
                stats.add("running_header_footer_removed")
                continue
            if _TOC_LINE.search(s):
                stats.add("toc_lines_removed")
                continue
            answer_run = False
            if ocr_page and _is_ocr_noise(s):
                stats.add("ocr_noise_lines_removed")
                continue
            ln = _INLINE_UNDERSCORES.sub("____", ln)
            ln = _BULLET_START.sub("• ", ln)
            ln = re.sub(r"[ \t]{2,}", " ", ln).rstrip()
            ln = re.sub(r"\s+([,;:])(?=\s)", r"\1", ln)
            kept.append(ln)
        kept = _dehyphenate(kept, stats)
        text = "\n".join(kept)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        result.append(text)
    return result, stats


def normalize_block_text(text: str, stats: NormStats | None = None) -> str:
    """Normalization for already-structured blocks (DOCX, PPTX, HTML)."""
    stats = stats or NormStats()
    text = normalize_unicode(text, stats)
    text = _INLINE_UNDERSCORES.sub("____", text)
    text = _BULLET_START.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
