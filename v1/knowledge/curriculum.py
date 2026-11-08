"""PNG curriculum recognisers (NEXA-M2-001).

Two jobs:
1. Draft SourceMetadata fields from the file naming conventions of the PNG STEM
   curriculum set (ported from v0 main.py parse_curriculum_filename and
   parse_docx_curriculum_filename, NEXA-MIG-001 reuse).
2. Detect curriculum structure inside text (THEMA, MODULE, Topic, Activity,
   outcome codes) so chunks carry their own curriculum position.

A filename only drafts metadata. The ingestion manifest supplies the governance
fields (approval, approver, access) and wins over anything inferred here.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from .metadata import AccessClassification, SourceType, Subject

SUBJECT_CODES = {
    "bio": Subject.BIOLOGY, "biology": Subject.BIOLOGY,
    "chem": Subject.CHEMISTRY, "chemistry": Subject.CHEMISTRY,
    "physics": Subject.PHYSICS, "phy": Subject.PHYSICS, "phys": Subject.PHYSICS,
    "math": Subject.MATHEMATICS, "maths": Subject.MATHEMATICS,
    "eng": Subject.ENGINEERING, "engineering": Subject.ENGINEERING,
    "tech": Subject.TECHNOLOGY, "technology": Subject.TECHNOLOGY,
    "english": Subject.ENGLISH,
}

DOCTYPE_CODES = {
    "syllabus": SourceType.OFFICIAL_SYLLABUS,
    "syllubus": SourceType.OFFICIAL_SYLLABUS,  # two real files carry this spelling
    "teacherguide": SourceType.TEACHER_GUIDE,
    "tg": SourceType.TEACHER_GUIDE,
    "trb": SourceType.TEACHER_GUIDE,
    "studentguide": SourceType.STUDENT_GUIDE,
    "srb": SourceType.STUDENT_GUIDE,
}

DEFAULT_ACCESS = {
    SourceType.OFFICIAL_SYLLABUS: AccessClassification.LEARNER,
    SourceType.TEACHER_GUIDE: AccessClassification.TEACHER_ONLY,
    SourceType.STUDENT_GUIDE: AccessClassification.LEARNER,
    SourceType.LESSON_NOTES: AccessClassification.LEARNER,
    SourceType.ASSESSMENT: AccessClassification.RESTRICTED,
    SourceType.LMS_CONTENT: AccessClassification.LEARNER,
    SourceType.TEACHER_MATERIAL: AccessClassification.TEACHER_ONLY,
    SourceType.EXTERNAL_REFERENCE: AccessClassification.LEARNER,
}

NDOE = "PNG National Department of Education"

_STEM_PDF_RE = re.compile(
    r"^STEM[\s_-]*(?P<subject>[A-Za-z]+)[\s_-]*Term[\s_-]*(?P<term>\d)"
    r"[\s_-]*(?P<doctype>[A-Za-z]+)[\s_-]*Gr(?:ade)?[\s_-]*(?P<grade>\d{1,2})$",
    re.IGNORECASE,
)
# s04_cm8879_12.2_Genetics and Molecular Biology_3_DNA Replication.docx
_LESSON_DOCX_RE = re.compile(
    r"^s(?P<seq>\d+)_cm(?P<cm>\d+)_(?P<module>(?:9|1[0-2])\.\d{1,2})_(?P<rest>.+)$",
    re.IGNORECASE,
)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def draft_from_filename(path: str) -> dict[str, Any]:
    """Return draft metadata fields inferred from the file name. Keys match
    SourceMetadata. Unknown fields are omitted, never guessed."""
    name = os.path.basename(path)
    stem, _ = os.path.splitext(name)
    out: dict[str, Any] = {"file_name": name}

    m = _STEM_PDF_RE.match(stem.strip())
    if m:
        subject = SUBJECT_CODES.get(m.group("subject").lower())
        stype = DOCTYPE_CODES.get(m.group("doctype").lower())
        grade, term = int(m.group("grade")), int(m.group("term"))
        if subject and stype:
            kind = {SourceType.OFFICIAL_SYLLABUS: "Syllabus",
                    SourceType.TEACHER_GUIDE: "Teacher Guide",
                    SourceType.STUDENT_GUIDE: "Student Guide"}[stype]
            out.update({
                "source_id": f"png-ndoe:stem-{_slug(subject.value)}-{_slug(kind)}-gr{grade}-t{term}",
                "source_type": stype,
                "subject": subject,
                "grade_levels": [grade],
                "term": term,
                "title": f"STEM {subject.value} {kind}, Grade {grade}, Term {term}",
                "scope": "national",
                "publisher": NDOE,
                "access_classification": DEFAULT_ACCESS[stype],
            })
        return out

    m = _LESSON_DOCX_RE.match(stem.strip())
    if m:
        module = m.group("module")
        grade = int(module.split(".")[0])
        rest = m.group("rest").replace("_", " ")
        rest = re.sub(r"\s+", " ", rest).strip()
        rest = re.sub(r"\s*-?\s*Grade \d+ Lesson Notes$|\s*Lesson Notes$", "", rest, flags=re.I).strip()
        # "Genetics and Molecular Biology 3 DNA Replication" -> module title, lesson no, topic
        lm = re.match(r"^(?P<mtitle>.+?)\s+(?P<n>\d+)\s+(?P<topic>.+)$", rest)
        mtitle, topic = (lm.group("mtitle"), lm.group("topic")) if lm else (rest, rest)
        cm = m.group("cm")
        out.update({
            "source_id": f"edunex:cm{cm}",
            "source_type": SourceType.LMS_CONTENT,
            "subject": Subject.BIOLOGY,  # the lesson-note set is Grade 12 Biology (M0-004)
            "grade_levels": [grade],
            "title": f"{topic.strip()} (Lesson Notes, {mtitle.strip()})",
            "scope": "national",
            "curriculum": {"module_code": module, "module_title": mtitle.strip(), "topic_title": topic.strip()},
            "lms": {"system": "edunex", "course_id": f"stem-biology-gr{grade}", "item_id": f"cm{cm}"},
            "access_classification": DEFAULT_ACCESS[SourceType.LMS_CONTENT],
        })
    return out


# ---- in-text structure ----

# Real PNG STEM headings are upper case ("THEMA: BATTERY", "MODULE 12.1: PROTEIN
# SEQUENCING"). Mixed-case "Thema" and "Module 12.1" lines are table cells and
# cross-references, so both keyword and title must be upper case.
THEMA_RE = re.compile(r"^\s*THEMA\b\s*[:\-–]?\s*(?P<title>[A-Z][A-Z0-9 &,/()\-–]{2,80})\s*$")
_THEMA_NOT_TITLES = re.compile(r"^(MODULE|TOPICS?)\b")
MODULE_RE = re.compile(
    r"^\s*MODULE\s+(?P<code>\d{1,2}(?:\.\d{1,2})?)\s*[:\-–]\s*(?P<title>[A-Z][A-Z0-9 &,/()\-–]{2,100}?)\s*$")
TOPIC_RE = re.compile(r"^\s*Topic\s+(?P<num>\d{1,2})\s*[:\-–.]\s*(?P<title>.{2,100}?)\s*$", re.IGNORECASE)
UNIT_RE = re.compile(r"^\s*(?:Unit|Lesson|Chapter)\s+(?P<num>[\d.]{1,6})\s*[:\-–.]\s*(?P<title>.{2,100}?)\s*$",
                     re.IGNORECASE)
ACTIVITY_RE = re.compile(r"^\s*(?:Activity|Exercise|Practice|Worked Example|Example|Assessment Task)"
                         r"\s*[\d.]*\s*[:\-–.]?\s*(?P<title>.{0,80})$", re.IGNORECASE)
OUTCOME_IN_TEXT_RE = re.compile(r"(?<![\d.])((?:9|1[0-2])\.\d{1,2}\.\d{1,2}[a-z]?)(?![\d])")
TOC_LINE_RE = re.compile(r"(?:\.\s?){6,}\s*[ivxlcdmIVXLCDM\d]+\s*$")


# Known spelling variants in the source documents, mapped to one value so the
# thema field filters cleanly. The heading text keeps the original wording.
THEMA_ALIASES = {"Biotehcnology": "Biotechnology"}


def canonical_thema(title: str) -> str:
    t = re.sub(r"\s*\([A-Z]{1,5}\)$", "", title).strip()
    return THEMA_ALIASES.get(t, t)


_MINOR = {"and", "or", "of", "the", "a", "an", "in", "on", "for", "to", "with", "by", "at", "from", "vs"}


def smart_title(text: str) -> str:
    """Title case for upper-case curriculum headings: minor words stay lower case
    and short parenthesised acronyms stay upper case ("Artificial Intelligence (AI)")."""
    out = []
    for i, w in enumerate(text.split()):
        core = w.strip("()")
        if w.startswith("(") and core.isalpha() and len(core) <= 4:
            out.append(w.upper())
        elif i and w.lower() in _MINOR:
            out.append(w.lower())
        else:
            out.append("-".join(part[:1].upper() + part[1:].lower() for part in w.split("-")))
    return " ".join(out)


def detect_heading(line: str) -> Optional[tuple[int, str, dict[str, Any]]]:
    """Recognise a curriculum heading. Returns (level, clean_title, curriculum_fields).
    Levels: 1 thema, 2 module, 3 topic/unit, 4 activity."""
    s = line.strip()
    if not s or len(s) > 120 or TOC_LINE_RE.search(s):
        return None
    m = THEMA_RE.match(s)
    if m and not _THEMA_NOT_TITLES.match(m.group("title")):
        t = smart_title(m.group("title").strip())
        return 1, f"Thema: {t}", {"thema": canonical_thema(t)}
    m = MODULE_RE.match(s)
    if m:
        code, t = m.group("code"), smart_title(m.group("title").strip())
        return 2, f"Module {code}: {t}", {"module_code": code, "module_title": t}
    m = TOPIC_RE.match(s)
    if m:
        t = m.group("title").strip()
        return 3, f"Topic {m.group('num')}: {t}", {"topic_num": int(m.group("num")), "topic_title": t}
    m = UNIT_RE.match(s)
    if m:
        return 3, s, {"topic_title": m.group("title").strip()}
    m = ACTIVITY_RE.match(s)
    if m and len(s) <= 80:
        return 4, s.rstrip(":"), {}
    return None


def outcome_codes(text: str) -> list[str]:
    seen: list[str] = []
    for c in OUTCOME_IN_TEXT_RE.findall(text):
        if c not in seen:
            seen.append(c)
    return seen
