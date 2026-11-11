"""Curriculum outline answers: "what are the topics in Grade 11 Biology?".

Search answers from a handful of passages, which suits specific questions but
not questions about the shape of a whole course: the passages that match "topics
in Grade 11 Biology" best are syllabus cover pages and forewords. NEXA already
holds the structure of every official syllabus it has ingested (the section
path of each chunk: Thema > Module 11.1: Agricultural Biodiversity), so an
outline question is answered from that structure instead:

1. detect(): the question asks what a course contains (topics, units,
   modules, themas, outline, "what will we learn") and names a subject the
   index holds a syllabus for; a grade and term are optional.
2. build(): for every approved official syllabus of that subject (and grade,
   term) the asker may see, the Themas and modules in syllabus order, with
   page references, plus the syllabus's own content-overview text.
3. The outlines become the sources [S1]..[Sn] of a normal grounded answer, one
   per syllabus document, each cited to that document. Terms NEXA does not
   hold for that grade are stated, so a partial outline never reads as the
   whole course.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .access import RetrievalFilter
from .retrieval import RetrievalResult

_OVERVIEW = re.compile(
    r"\b(topics?|units?|modules?|themas?|themes|strands?|outline|syllabus content|content overview|"
    r"scope and sequence|course content)\b"
    r"|\bwhat (?:will|do|does|did|are|is) (?:i|we|students|you|grade \d+ students) (?:learn|study|cover|covering)\b"
    r"|\bwhat(?:'s| is) (?:in|covered in) (?:the )?(?:grade|gr\.?)\s*\d+",
    re.I)
_GRADE = re.compile(r"\b(?:grade|gr\.?|year)\s*(\d{1,2})\b", re.I)
_TERM = re.compile(r"\bterm\s*(\d)\b", re.I)
_MODULE = re.compile(r"^\s*Module\s*(\d+(?:\.\d+)*)?\s*[:\-–]?\s*(.*\S)?\s*$", re.I)
_THEMA = re.compile(r"^\s*Them[ae]\s*[:\-–]\s*(.+\S)\s*$", re.I)
_OVERVIEW_SECTION = re.compile(r"content overview|thema sequence", re.I)
_ALIASES = {"maths": "Mathematics", "math": "Mathematics", "bio": "Biology", "chem": "Chemistry",
            "physic": "Physics"}

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
CACHE_TTL_S = 300


@dataclass
class Ask:
    subject: str
    grade: Optional[int] = None
    term: Optional[int] = None


@dataclass
class Syllabus:
    item: dict[str, Any]                 # representative chunk (its provenance is the citation)
    subject: str
    grade: Optional[int]
    term: Optional[int]
    title: str
    themas: list[tuple[str, list[tuple[str, str, Optional[str]]]]] = field(default_factory=list)
    overview: str = ""


def _grade_of(md: dict[str, Any]) -> Optional[int]:
    g = md.get("grade_levels")
    if isinstance(g, (list, tuple)):
        g = g[0] if g else None
    try:
        return int(str(g).split(",")[0])
    except (TypeError, ValueError):
        return None


def _term_of(md: dict[str, Any]) -> Optional[int]:
    try:
        return int(md["term"]) if md.get("term") is not None else None
    except (TypeError, ValueError):
        return None


def detect(question: str, subjects: set[str]) -> Optional[Ask]:
    if not _OVERVIEW.search(question):
        return None
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", question)}
    subject = next((s for s in sorted(subjects) if s.lower() in words), None)
    if subject is None:
        subject = next((_ALIASES[w] for w in words if w in _ALIASES and _ALIASES[w] in subjects), None)
    if subject is None:
        return None
    g, t = _GRADE.search(question), _TERM.search(question)
    return Ask(subject, int(g.group(1)) if g else None, int(t.group(1)) if t else None)


async def _syllabus_chunks(store, index_id: str) -> list[dict[str, Any]]:
    hit = _CACHE.get(index_id)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    items = await store.list_by_source_type(index_id, "official_syllabus")
    _CACHE[index_id] = (time.time(), items)
    return items


def clear_cache() -> None:
    _CACHE.clear()


def _outline(chunks: list[dict[str, Any]]) -> tuple[list, str, dict[str, Any]]:
    chunks = sorted(chunks, key=lambda c: (int((c.get("metadata") or {}).get("ordinal") or 0)))
    themas: list[tuple[str, list]] = []
    seen: set[tuple[str, str]] = set()
    overview, rep, in_overview = [], None, False
    for c in chunks:
        md = c.get("metadata") or {}
        parts = [p.strip() for p in str(md.get("section_path") or "").split(" > ") if p.strip()]
        thema = next((m.group(1) for p in parts if (m := _THEMA.match(p))), None)
        mod = next((m for p in parts if (m := _MODULE.match(p))), None)
        # the term's content overview, which can run over a following chunk or two
        if any(_OVERVIEW_SECTION.search(p) for p in parts) and not overview:
            in_overview, rep = True, c
        elif thema or mod:
            in_overview = False
        if in_overview and sum(len(x) for x in overview) < 1500:
            overview.append(" ".join((c.get("text") or "").split()))
        if not mod:
            continue
        code, name = mod.group(1) or "", (mod.group(2) or "").strip()
        key = (thema or "", code + name)
        if not name or key in seen:
            continue
        seen.add(key)
        if not themas or themas[-1][0] != (thema or ""):
            themas.append((thema or "", []))
        themas[-1][1].append((code, name, md.get("page_label") or md.get("page_start")))
        rep = rep or c
    return themas, " ".join(overview)[:1500], rep or (chunks[0] if chunks else {})


async def build(index_mgr, where: RetrievalFilter, question: str) -> Optional[tuple[RetrievalResult, str, list[str]]]:
    """(result whose items are syllabus outlines, augmented question, notes),
    or None when the question is not an outline question or nothing matches."""
    rec = await index_mgr.active()
    store = index_mgr.store
    if rec is None or not hasattr(store, "list_by_source_type"):
        return None
    try:
        chunks = await _syllabus_chunks(store, rec.index_id)
    except NotImplementedError:
        return None
    visible = [c for c in chunks if where(c.get("metadata") or {})]
    ask = detect(question, {(c.get("metadata") or {}).get("subject") for c in visible} - {None})
    if ask is None:
        return None
    docs: dict[str, list[dict[str, Any]]] = {}
    for c in visible:
        md = c.get("metadata") or {}
        docs.setdefault(md.get("version_id") or md.get("source_id") or md.get("title"), []).append(c)
    sylls: list[Syllabus] = []
    terms_by_grade: dict[Optional[int], set[int]] = {}
    for cs in docs.values():
        md = cs[0].get("metadata") or {}
        g, t = _grade_of(md), _term_of(md)
        if t is not None:
            terms_by_grade.setdefault(g, set()).add(t)
        if md.get("subject") != ask.subject or (ask.grade and g != ask.grade) or (ask.term and t != ask.term):
            continue
        themas, overview, rep = _outline(cs)
        if not themas and not overview:
            continue
        sylls.append(Syllabus(item=rep, subject=ask.subject, grade=g, term=t, title=md.get("title") or "",
                              themas=themas, overview=overview))
    if not sylls:
        return None
    sylls.sort(key=lambda s: (s.grade or 0, s.term or 0, s.title))
    results = []
    for i, s in enumerate(sylls, 1):
        lines = [f"Outline of {s.title} (official syllabus, PNG National Department of Education)."]
        for thema, mods in s.themas:
            lines.append(f"Thema: {thema}" if thema else "Modules:")
            for code, name, page in mods:
                lines.append(f"  - Module{' ' + code if code else ''}: {name}" + (f" (p. {page})" if page else ""))
        if s.overview:
            lines.append("Content overview as printed in the syllabus: " + s.overview)
        md = dict(s.item.get("metadata") or {})
        md["citation"] = f"{s.title}, content overview" + (f", p. {md.get('page_label')}" if md.get("page_label") else "")
        results.append({"chunk_id": s.item.get("chunk_id"), "text": "\n".join(lines), "metadata": md, "rank": i,
                        "score": 1.0, "scores": {"similarity": 1.0}, "coverage": {"coverage": 1.0},
                        "authority": {"authoritative": True, "notes": []},
                        "reasons": ["curriculum outline built from the syllabus structure"]})
    notes = []
    for g in sorted({s.grade for s in sylls}, key=lambda x: x or 0):
        have = sorted({s.term for s in sylls if s.grade == g and s.term is not None})
        expected = set().union(*terms_by_grade.values()) | set(have)
        missing = sorted(expected - set(have)) if not ask.term else []
        if have:
            gtxt = f"Grade {g} " if g else ""
            msg = (f"NEXA holds the {gtxt}{ask.subject} syllabus for Term{'s' if len(have) > 1 else ''} "
                   + ", ".join(map(str, have)))
            if missing:
                msg += f"; Term{'s' if len(missing) > 1 else ''} {', '.join(map(str, missing))} "
                msg += "not yet available in NEXA"
            notes.append(msg + ".")
    explain = {"mode": "curriculum_outline", "subject": ask.subject, "grade": ask.grade, "term": ask.term,
               "syllabi": [s.title for s in sylls], "timings_ms": {"total": 0.0}, "conflicts": []}
    q = (question + "\n\nThe sources are the outlines of the official PNG syllabi NEXA holds for this course. "
         "Answer by listing, term by term, each Thema and its modules with their module numbers, and cite each "
         "term's source. Do not add topics that are not in the sources.")
    return RetrievalResult(results=results, explain=explain, index=rec), q, notes
