"""Rebuild document structure from flat text lines (NEXA-M2-003).

PDF and OCR text arrives as wrapped lines with no markup. This turns it into
Blocks (heading, paragraph, list item, exercise, equation) so the chunker can
cut on meaning instead of on character counts.
"""
from __future__ import annotations

import re
from typing import Optional

from ..curriculum import detect_heading
from ..ingestion.model import Block, BlockKind

_LIST_MARKER = re.compile(
    r"^\s*(?:•|[-*–]\s|\(?\d{1,2}[.)]\s|\(?[a-hA-H][.)]\s|\(?(?:i{1,3}|iv|v|vi{1,3}|ix|x)[.)]\s)")
_QUESTION = re.compile(r"^\s*(?:Q(?:uestion)?\s*\d+[.:)]?|\d{1,2}[.)])\s+\S")
_NUMBERED_HEADING = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){1,3})\s+([A-Z][^.]{2,80})$")
_EQUATION_HINT = re.compile(r"(=|→|⇌|⇄|≤|≥|±|√|∑|∫|Δ|×|÷|\^)")
_TERMINAL = re.compile(r"[.!?:;)\"']\s*$")


def _is_equation(line: str, aggressive: bool) -> bool:
    s = line.strip()
    if not s or len(s) > 140 or not _EQUATION_HINT.search(s):
        return False
    words = re.findall(r"[A-Za-z]{3,}", s)
    letters = sum(ch.isalpha() for ch in s)
    limit = 6 if aggressive else 4
    return len(words) <= limit and letters / max(len(s), 1) < 0.65


_DANGLING = re.compile(r"\b(?:and|or|of|the|a|an|in|on|for|to|with|by|from|at|as|is|are|using|into)\s*$", re.I)


def _generic_heading(line: str, nxt: Optional[str], prev: Optional[str] = None) -> Optional[tuple[int, str]]:
    s = line.strip()
    if not (3 <= len(s) <= 90) or _TERMINAL.search(s) and not s.endswith(":"):
        return None
    # A heading does not run on into a lowercase continuation or end mid-phrase.
    if _DANGLING.search(s.rstrip(":")) or (nxt and nxt.strip()[:1].islower()):
        return None
    # Wrapped table cells: a heading starts after a blank line or a finished sentence.
    if prev is not None and prev.strip() and not _TERMINAL.search(prev.strip()) and not s.isupper():
        return None
    if s.endswith(":") and len(s.split()) > 6:
        return None
    if _LIST_MARKER.match(s) and not _NUMBERED_HEADING.match(s):
        return None
    words = s.rstrip(":").split()
    if not words or len(words) > 12:
        return None
    m = _NUMBERED_HEADING.match(s)
    if m:
        depth = m.group(1).count(".") + 1
        return min(2 + depth, 5), s.rstrip(":")
    alpha = [w for w in words if re.match(r"[A-Za-z]", w)]
    if not alpha:
        return None
    if s.isupper() and _NAME_LINE.search(s):
        return None
    if s.isupper() and len(s) >= 4:
        return 2, s.rstrip(":").title() if len(s) > 30 else s.rstrip(":")
    small = {"and", "or", "of", "the", "a", "an", "in", "on", "for", "to", "with", "by", "vs", "at", "from"}
    titled = sum(1 for w in alpha if w[0].isupper() or w.lower() in small)
    if len(alpha) < 2:
        return None  # a lone title-case word is too often a table cell
    if titled / len(alpha) >= 0.85 and nxt is not None and len(nxt.strip()) > len(s) * 1.5:
        return 3, s.rstrip(":")
    return None


_CAPS_HEAD = re.compile(r"^\s*(?:(?:THEMA|MODULE)\b.*\b(?:AND|OF|&|–|-|THE|FOR|IN)|THEMA\s*:?|MODULE\s+\d{1,2}(?:\.\d{1,2})?\s*:)\s*$")
# Signature and name lines ("DR UKE KOMBRA, PHD") are upper case but are not headings.
_NAME_LINE = re.compile(r"\b(?:DR|MR|MRS|MS|PROF|PHD|PH\.D|MSC|BSC|MBA|MP|HON)\b\.?")
_CAPS_LINE = re.compile(r"^[A-Z0-9 &,/()\-–]{3,60}$")


_CAPS_TITLE_HEAD = re.compile(r"^\s*(?:THEMA\s*:?|MODULE\s+\d{1,2}(?:\.\d{1,2})?\s*:)\s*[A-Z][A-Z &,/()\-–]*$")
_CAPS_TAIL = re.compile(r"^[A-Z(][A-Z()\-–]*(?:\s+[A-Z(][A-Z()\-–]*){0,2}$")
_NOT_TAIL = re.compile(r"^(?:THEMA|MODULE|TOPICS?|CONTENT|CONTENTS|PERFORMANCE|ACTIVITY|ACTIVITIES|INTRODUCTION|"
                       r"OVERVIEW|RATIONALE|SUMMARY|GLOSSARY|REFERENCES|ASSESSMENT|APPENDIX|UNIT|LESSON)\b")
_MINOR_WORDS = {"and", "or", "of", "the", "for", "in", "on", "to", "with", "ai", "ict", "dna", "rna"}
_MAX_HEADING_WORDS = 8


def _join_wrapped_caps(lines: list[str]) -> list[str]:
    """Rejoin THEMA and MODULE headings wrapped over two or more lines
    ("THEMA:" / "ARTIFICIAL" / "INTELLIGENCE (AI)", "THEMA: BIOTECHNO-" / "LOGY").
    A joined line is re-examined so a heading split three ways joins fully."""
    lines = list(lines)
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        joinable = nxt and len((ln + " " + nxt).split()) <= _MAX_HEADING_WORDS and (
            (_CAPS_HEAD.match(ln) and _CAPS_LINE.match(nxt))
            or (_CAPS_TITLE_HEAD.match(ln) and _CAPS_TAIL.match(nxt) and not _NOT_TAIL.match(nxt)))
        if joinable:
            head = ln.rstrip()
            # A word broken by layout with no hyphen ("BIOTECHNO" / "LOGY"): a lone
            # short fragment that is not a word in its own right is glued on.
            glue = head.endswith(("-", "–")) or (
                re.fullmatch(r"[A-Z]{2,4}", nxt) is not None and nxt.lower() not in _MINOR_WORDS
                and len(head.split()[-1]) >= 5 and not head.rstrip(": ").endswith(("THEMA", "MODULE")))
            lines[i + 1] = (head.rstrip("-–") + nxt) if glue else head + " " + nxt
            i += 1
            continue
        out.append(ln)
        i += 1
    return out


def join_cross_page_headings(pages: list[str]) -> list[str]:
    """A THEMA or MODULE heading can end one page and finish on the next
    ("THEMA: ARTIFICIAL" | "INTELLIGENCE"). Move the tail back so the heading
    is recognised whole on the page where it starts."""
    out = list(pages)
    for i in range(len(out) - 1):
        lines = out[i].rstrip().split("\n")
        nxt = out[i + 1].lstrip("\n").split("\n")
        if not lines or not nxt:
            continue
        last, first = lines[-1], nxt[0].strip()
        if not first:
            continue
        joined = _join_wrapped_caps([last, first])
        if len(joined) == 1:
            lines[-1] = joined[0]
            out[i] = "\n".join(lines)
            out[i + 1] = "\n".join(nxt[1:])
    return out


def structure_lines(text: str, aggressive_equations: bool = False) -> list[Block]:
    """Turn a normalized page of text into Blocks."""
    lines = _join_wrapped_caps(text.split("\n"))
    nonblank = [len(ln) for ln in lines if ln.strip()]
    typical = sorted(nonblank)[int(len(nonblank) * 0.8)] if nonblank else 80
    blocks: list[Block] = []
    buf: list[str] = []
    buf_kind = BlockKind.PARAGRAPH
    in_exercise = False

    def flush():
        nonlocal buf, buf_kind
        if buf:
            txt = " ".join(x.strip() for x in buf).strip()
            if txt:
                blocks.append(Block(buf_kind, txt, level=1 if buf_kind == BlockKind.LIST_ITEM else 0))
        buf, buf_kind = [], BlockKind.PARAGRAPH

    for i, raw in enumerate(lines):
        s = raw.strip()
        nxt = next((x for x in lines[i + 1:] if x.strip()), None)
        prev = lines[i - 1] if i > 0 else None
        if not s:
            flush()
            continue
        if s == "[answer space]":
            flush()
            if blocks and blocks[-1].kind in (BlockKind.EXERCISE, BlockKind.LIST_ITEM, BlockKind.PARAGRAPH):
                blocks[-1].text += " [answer space]"
                if in_exercise:
                    blocks[-1].kind = BlockKind.EXERCISE
            continue
        h = detect_heading(s)
        if h:
            flush()
            level, title, fields = h
            blocks.append(Block(BlockKind.HEADING, title, level=level,
                                meta={"curriculum_heading": True, "curriculum": fields}))
            in_exercise = level == 4 or bool(re.search(r"exercise|question|activity|assessment", title, re.I))
            continue
        g = None if buf and not _TERMINAL.search(buf[-1]) and len(buf[-1]) >= typical * 0.8 else _generic_heading(s, nxt, prev)
        if g:
            flush()
            blocks.append(Block(BlockKind.HEADING, g[1], level=g[0]))
            in_exercise = bool(re.search(r"exercise|questions?\b|activity|assessment|practice", g[1], re.I))
            continue
        if _is_equation(s, aggressive_equations):
            flush()
            blocks.append(Block(BlockKind.EQUATION, s))
            continue
        if _LIST_MARKER.match(s) or (in_exercise and _QUESTION.match(s)):
            flush()
            buf = [s]
            buf_kind = BlockKind.EXERCISE if in_exercise and _QUESTION.match(s) else BlockKind.LIST_ITEM
            continue
        buf.append(s)
        # A short line that ends a sentence usually ends a paragraph.
        if _TERMINAL.search(s) and len(s) < typical * 0.7:
            flush()
    flush()
    return blocks
