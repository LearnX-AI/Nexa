"""Curriculum outline answers: "what are the topics in Grade 11 Biology?".

Search answers from a handful of passages, which suits specific questions but
not questions about the shape of a whole course: the passages that match "topics
in Grade 11 Biology" best are syllabus cover pages and forewords. NEXA already
holds the structure of every document it has ingested (the section path of each
chunk: Thema > Module 11.1: Agricultural Biodiversity > Topic 1: ...), so an
outline question is answered from that structure instead:

1. detect(): the question asks what a course contains (topics, units,
   modules, themas, outline, "what will we learn") and names a subject NEXA
   holds material for; a grade and term are optional.
2. build(): for each term of that subject (and grade) the asker may see, the
   structure of the best document NEXA holds for that term: the official
   syllabus, else the Student Guide, else the Teacher Guide (staff only). Each
   gives its Themas, modules and topics in order, with pages. The EduNex course
   for that subject and grade is added as its own source, its lessons grouped
   by unit.
3. The outlines become the sources [S1]..[Sn] of a normal grounded answer, each
   cited to its document. NEXA then states which document each term came from
   and which terms it does not hold, so a partial outline never reads as the
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
_TOPIC = re.compile(r"^\s*Topic\s*(\d+)\s*[:\-–.]\s*(.+\S)\s*$", re.I)
_LESSON = re.compile(r"^(.*?)\s*\((?:Lesson Notes?|Notes),\s*(.+)\)\s*$", re.I)
_LMS_SUFFIX = re.compile(r"[\s_-]*(lecture[\s_]*ppt|ppt|teacher[\s_]*notes?|teature[\s_]*notes?|lesson[\s_]*notes?|"
                         r"lessonnote|slides?)\s*$", re.I)
# Documents that can give a term's structure, best first.
STRUCTURE_TYPES = ("official_syllabus", "student_guide", "teacher_guide", "teacher_material")
KIND = {"official_syllabus": "syllabus", "student_guide": "Student Guide", "teacher_guide": "Teacher Guide",
        "teacher_material": "teacher material", "lms_content": "EduNex course"}
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


async def _chunks(store, index_id: str, source_type: str) -> list[dict[str, Any]]:
    key = f"{index_id}|{source_type}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    items = await store.list_by_source_type(index_id, source_type)
    _CACHE[key] = (time.time(), items)
    return items


def clear_cache() -> None:
    _CACHE.clear()


def _outline(chunks: list[dict[str, Any]]) -> tuple[list, str, dict[str, Any]]:
    """[(thema, [(module code, module name, page, [topics])])], overview text,
    representative chunk."""
    chunks = sorted(chunks, key=lambda c: (int((c.get("metadata") or {}).get("ordinal") or 0)))
    themas: list[tuple[str, list]] = []
    seen: set[tuple[str, str]] = set()
    overview, rep, in_overview = [], None, False
    for c in chunks:
        md = c.get("metadata") or {}
        parts = [p.strip() for p in str(md.get("section_path") or "").split(" > ") if p.strip()]
        thema = next((m.group(1) for p in parts if (m := _THEMA.match(p))), None)
        mod = next((m for p in parts if (m := _MODULE.match(p))), None)
        topic = next((m for p in parts if (m := _TOPIC.match(p))), None)
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
        if not name:
            continue
        key = (thema or "", code + name)
        if key not in seen:
            seen.add(key)
            if not themas or themas[-1][0] != (thema or ""):
                themas.append((thema or "", []))
            themas[-1][1].append((code, name, md.get("page_label") or md.get("page_start"), []))
        rep = rep or c
        if topic:
            for t, mods in themas:
                for m in mods:
                    if t == (thema or "") and m[0] + m[1] == code + name:
                        label = f"Topic {topic.group(1)}: {topic.group(2)}"
                        if label not in m[3]:
                            m[3].append(label)
    return themas, " ".join(overview)[:1500], rep or (chunks[0] if chunks else {})


def _lesson_name(title: str) -> tuple[str, Optional[str]]:
    m = _LESSON.match(title or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    t = _LMS_SUFFIX.sub("", (title or "").replace("_", " ")).strip()
    return re.sub(r"\s{2,}", " ", t), None


def _lms_order(md: dict[str, Any]) -> tuple[int, str]:
    m = re.search(r"cm(\d+)", str(md.get("source_uri") or md.get("file_name") or md.get("source_id") or ""))
    return (int(m.group(1)) if m else 10 ** 9, str(md.get("title") or ""))


def _lms_outline(chunks: list[dict[str, Any]]) -> tuple[list[tuple[str, list[str]]], dict[str, Any]]:
    by_doc: dict[str, dict[str, Any]] = {}
    for c in chunks:
        md = c.get("metadata") or {}
        k = md.get("version_id") or md.get("source_id") or md.get("title")
        if k not in by_doc or int(md.get("ordinal") or 0) < int((by_doc[k].get("metadata") or {}).get("ordinal") or 0):
            by_doc[k] = c
    units: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for c in sorted(by_doc.values(), key=lambda c: _lms_order(c.get("metadata") or {})):
        name, unit = _lesson_name((c.get("metadata") or {}).get("title") or "")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        unit = unit or "Other lessons"
        for u, lessons in units:
            if u == unit:
                lessons.append(name)
                break
        else:
            units.append((unit, [name]))
    rep = min(by_doc.values(), key=lambda c: _lms_order(c.get("metadata") or {})) if by_doc else {}
    return units, rep


async def build(index_mgr, where: RetrievalFilter, question: str) -> Optional[tuple[RetrievalResult, str, list[str]]]:
    """(result whose items are course outlines, augmented question, notes),
    or None when the question is not an outline question or nothing matches."""
    rec = await index_mgr.active()
    store = index_mgr.store
    if rec is None or not hasattr(store, "list_by_source_type"):
        return None
    try:
        everything = {t: await _chunks(store, rec.index_id, t) for t in STRUCTURE_TYPES + ("lms_content",)}
    except NotImplementedError:
        return None
    pools = {t: [c for c in cs if where(c.get("metadata") or {})] for t, cs in everything.items()}
    subjects = {(c.get("metadata") or {}).get("subject") for cs in pools.values() for c in cs} - {None}
    ask = detect(question, subjects)
    if ask is None:
        return None

    def wanted(md: dict[str, Any]) -> bool:
        return md.get("subject") == ask.subject and (not ask.grade or _grade_of(md) == ask.grade)

    # the terms a course has: every term any syllabus or guide in the index covers (whoever may see it)
    all_terms = {_term_of(c.get("metadata") or {}) for t in STRUCTURE_TYPES for c in everything[t]} - {None}
    best: dict[tuple[Optional[int], int], tuple[str, Syllabus]] = {}
    for stype in STRUCTURE_TYPES:
        docs: dict[str, list[dict[str, Any]]] = {}
        for c in pools[stype]:
            md = c.get("metadata") or {}
            if wanted(md) and _term_of(md) is not None and (not ask.term or _term_of(md) == ask.term):
                docs.setdefault(md.get("version_id") or md.get("source_id") or md.get("title"), []).append(c)
        for cs in docs.values():
            md = cs[0].get("metadata") or {}
            key = (_grade_of(md), _term_of(md))
            if key in best:
                continue  # a better document already gives this term
            themas, overview, rep = _outline(cs)
            if themas or (stype == "official_syllabus" and overview):
                best[key] = (stype, Syllabus(item=rep, subject=ask.subject, grade=key[0], term=key[1],
                                             title=md.get("title") or "", themas=themas, overview=overview))
    lms = [c for c in pools["lms_content"] if wanted(c.get("metadata") or {})]
    units, lms_rep = _lms_outline(lms) if lms else ([], {})
    if not best and not units:
        return None

    results = []
    for (g, t), (stype, s) in sorted(best.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1])):
        kind = KIND[stype]
        lines = [f"Grade {g} Term {t} outline, from the {kind}: {s.title}."]
        for thema, mods in s.themas:
            lines.append(f"Thema: {thema}" if thema else "Modules:")
            for code, name, page, topics in mods:
                lines.append(f"  - Module{' ' + code if code else ''}: {name}" + (f" (p. {page})" if page else ""))
                lines += [f"      - {tp}" for tp in topics]
        if s.overview:
            lines.append("Content overview as printed in the syllabus: " + s.overview)
        md = dict(s.item.get("metadata") or {})
        md["citation"] = s.title + (f", p. {md.get('page_label')}" if md.get("page_label") else "")
        results.append({"chunk_id": s.item.get("chunk_id"), "text": "\n".join(lines), "metadata": md,
                        "rank": len(results) + 1, "score": 1.0, "scores": {"similarity": 1.0},
                        "coverage": {"coverage": 1.0}, "authority": {"authoritative": stype == "official_syllabus",
                                                                     "notes": []},
                        "reasons": [f"course outline built from the {kind} structure"]})
    if units:
        md = dict(lms_rep.get("metadata") or {})
        course = md.get("publisher")
        md["citation"] = f"EduNex course: {course}" if course else "EduNex course lessons"
        lines = [f"Lessons in the EduNex course{' ' + course if course else ''}, grouped by unit:"]
        n = 0
        for unit, lessons in units:
            lines.append(f"Unit: {unit}")
            for name in lessons:
                if n >= 80:
                    break
                lines.append(f"  - {name}")
                n += 1
        results.append({"chunk_id": lms_rep.get("chunk_id"), "text": "\n".join(lines), "metadata": md,
                        "rank": len(results) + 1, "score": 1.0, "scores": {"similarity": 1.0},
                        "coverage": {"coverage": 1.0}, "authority": {"authoritative": False, "notes": []},
                        "reasons": ["course outline built from the EduNex course lessons"]})

    notes = []
    for g in sorted({k[0] for k in best} | ({ask.grade} if ask.grade else set()), key=lambda x: x or 0):
        have = {t: KIND[st] for (gg, t), (st, _) in best.items() if gg == g}
        expected = ({ask.term} if ask.term else all_terms | set(have))
        gtxt = f"Grade {g} " if g else ""
        held = []
        for kind in dict.fromkeys(have[t] for t in sorted(have)):
            ts = [t for t in sorted(have) if have[t] == kind]
            held.append(f"Term{'s' if len(ts) > 1 else ''} {', '.join(map(str, ts))} from the {kind}")
        if units:
            held.append("the EduNex course lessons")
        missing = sorted(expected - set(have))
        msg = f"{gtxt}{ask.subject} in NEXA: " + (", ".join(held) if held else "no term outline") + "."
        if missing:
            msg += f" No outline available yet for Term{'s' if len(missing) > 1 else ''} {', '.join(map(str, missing))}."
        notes.append(msg)
    explain = {"mode": "curriculum_outline", "subject": ask.subject, "grade": ask.grade, "term": ask.term,
               "sources": [r["metadata"].get("citation") for r in results], "timings_ms": {"total": 0.0},
               "conflicts": []}
    q = (question + "\n\nThe sources are outlines of the PNG curriculum documents NEXA holds for this course, one "
         "per term, and the EduNex course lessons. Answer by listing, term by term, each Thema with its modules "
         "(and topics where given), then the EduNex course units with their lessons. Cite each source. Do not add "
         "topics that are not in the sources, and do not add notes about missing terms or sources: NEXA adds those.")
    return RetrievalResult(results=results, explain=explain, index=rec), q, notes
