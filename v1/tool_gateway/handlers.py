"""Tool implementations behind the contracts (NEXA-M3-004).

builtin handlers run inside the gateway; http handlers use a connector. A
handler receives the validated arguments and a Call (the user, the scopes
granted, a connection), and returns a JSON-safe result with a short "text".
MCP tools need no handler: the gateway calls the bound MCP tool.
"""
from __future__ import annotations

import ast
import collections
import math
import operator
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from fractions import Fraction
from typing import Any, Awaitable, Callable, Optional

from v1.identity.principal import Principal

from .connectors import Connection, ConnectorError


@dataclass
class Call:
    principal: Principal
    scopes: list[str]
    connection: Optional[Connection]
    trace_id: Optional[str]


class HandlerError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


Handler = Callable[[dict[str, Any], Call], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(name: str):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


# ---------------------------------------------------------------- calculator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod}
_FUN = {"sqrt", "abs", "round"}
MAX_EXPONENT = 200
MAX_DIGITS = 400


def evaluate(expr: str) -> Fraction | float:
    """Exact arithmetic on rationals: + - * / ^ (or **) % ( ), sqrt, abs, round.
    A percentage like 15% means 15/100. Nothing else is evaluated."""
    text = expr.replace("×", "*").replace("÷", "/").replace("^", "**").replace(",", "")
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise HandlerError(422, "not an arithmetic expression") from None

    def ev(n) -> Fraction | float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return Fraction(str(n.value))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow):
                if not (isinstance(b, Fraction) and b.denominator == 1) or abs(b) > MAX_EXPONENT:
                    raise HandlerError(422, f"exponents must be whole numbers up to {MAX_EXPONENT}")
            if isinstance(n.op, (ast.Div, ast.Mod)) and b == 0:
                raise HandlerError(422, "division by zero")
            out = _BIN[type(n.op)](a, b)
            if isinstance(out, Fraction) and max(len(str(out.numerator)), len(str(out.denominator))) > MAX_DIGITS:
                raise HandlerError(422, "the result is too large")
            return out
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUN and len(n.args) == 1 \
                and not n.keywords:
            v = ev(n.args[0])
            if n.func.id == "abs":
                return abs(v)
            if n.func.id == "round":
                return Fraction(round(v))
            if v < 0:
                raise HandlerError(422, "square root of a negative number")
            r = math.isqrt(int(v)) if isinstance(v, Fraction) and v.denominator == 1 else None
            if r is not None and r * r == v:
                return Fraction(r)
            return math.sqrt(float(v))
        raise HandlerError(422, "only numbers, + - * / ^ % ( ), sqrt, abs and round are allowed")
    return ev(tree)


def _fmt(v: Fraction | float) -> dict[str, Any]:
    getcontext().prec = 28
    if isinstance(v, float):
        return {"exact": None, "decimal": repr(round(v, 12)), "is_exact": False}
    dec = Decimal(v.numerator) / Decimal(v.denominator)
    dec_s = format(dec.normalize(), "f") if dec == dec.to_integral() else str(round(dec, 12)).rstrip("0").rstrip(".")
    exact = str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"
    return {"exact": exact, "decimal": dec_s, "is_exact": True}


@handler("calculator")
async def calculator(args: dict[str, Any], call: Call) -> dict[str, Any]:
    out = _fmt(evaluate(args["expression"]))
    if not out["is_exact"]:
        text = f"{args['expression']} ≈ {out['decimal']}"
    elif out["exact"] == out["decimal"]:
        text = f"{args['expression']} = {out['exact']}"
    else:
        text = f"{args['expression']} = {out['exact']} = {out['decimal']}"
    return {"expression": args["expression"], **out, "text": text}


@handler("current_time")
async def current_time(args: dict[str, Any], call: Call) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"utc": now, "text": now}


# ---------------------------------------------------------------- Knowledge Service

def _citations(body: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in (body.get("citations") or [])[:12]:
        if isinstance(c, dict):
            p = c.get("provenance") or {}
            out.append({"marker": c.get("marker"), "used": c.get("used"), "title": p.get("title"),
                        "page": p.get("page_label") or p.get("page_start"), "chunk_id": p.get("chunk_id")})
    return out


async def _answer(call: Call, body: dict[str, Any]) -> dict[str, Any]:
    try:
        status, data = await call.connection.request("POST", "/v1/knowledge/answer", json_body=body)
    except ConnectorError:
        raise
    if status != 200:
        raise HandlerError(502 if status >= 500 else status,
                           f"knowledge service answered {status}: {str(data.get('detail', ''))[:200]}")
    return {"status": data.get("status"), "answer": data.get("answer", ""), "confidence": data.get("confidence"),
            "citations": _citations(data), "evidence": (data.get("evidence") or {}).get("level"),
            "text": data.get("answer", "")}


@handler("knowledge_answer")
async def knowledge_answer(args: dict[str, Any], call: Call) -> dict[str, Any]:
    audience = args.get("audience")
    if audience == "teacher" and "curriculum.teacher" not in call.scopes:
        raise HandlerError(403, "teacher material needs the curriculum.teacher scope")
    body = {"question": args["question"], "general": bool(args.get("general", False))}
    if audience:
        body["audience"] = audience
    return await _answer(call, body)


@handler("course_outline")
async def course_outline(args: dict[str, Any], call: Call) -> dict[str, Any]:
    grade = f"Grade {args['grade']} " if args.get("grade") else ""
    term = f" for Term {args['term']}" if args.get("term") else ""
    q = f"What are the topics in {grade}{args['subject']}{term}?"
    body = {"question": q, "general": False}
    if "curriculum.teacher" in call.scopes:
        body["audience"] = "teacher"
    return await _answer(call, body) | {"asked": q}


# ---------------------------------------------------------------- teaching documents (NEXA-M4-003)

_KINDS = {"lesson_plan": "Lesson plan", "differentiation": "Differentiated lesson", "quiz": "Quiz with answer key",
          "rubric": "Assessment rubric", "resources": "Teaching resources", "alignment": "Curriculum alignment check",
          "student_support": "Student support suggestions",
          # NEXA-M4-005 Assessment & Feedback
          "questions": "Formative questions with answer guide", "assessment": "Formative assessment draft",
          "answer_guide": "Answer guide and rubric suggestions", "feedback": "Feedback explanation",
          "align": "Assessment alignment to curriculum outcomes"}
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])(?:\+?675[ -]?)?7\d{3}[ -]?\d{4}(?![\w])")
# Public service numbers the Teaching Intelligence Policy names (a test keeps
# this equal to the policy's public_contacts): not personal data.
PUBLIC_NUMBERS = {"71508000"}


@handler("teaching_document")
async def teaching_document(args: dict[str, Any], call: Call) -> dict[str, Any]:
    """A teacher-approved draft as a document with its sources and provenance.
    Personal contact details are refused: documents describe students, never
    identify them. Nothing is stored; the caller keeps the document."""
    import hashlib
    body, sources = args["body"].strip(), (args.get("sources") or "").strip()
    phones = [m.group(0) for m in _PHONE.finditer(body)
              if not any(re.sub(r"\D", "", m.group(0)).endswith(n) for n in PUBLIC_NUMBERS)]
    if _EMAIL.search(body) or phones:
        raise HandlerError(422, "the document contains personal contact details; remove them and try again")
    digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    kind = _KINDS[args["kind"]]
    reviewed = (args.get("reviewed_by") or "").strip()
    status = f"reviewed and approved by {reviewed}" if reviewed else "draft for teacher review"
    lines = [f"# {args['title'].strip()}", "", f"{kind} · Grade {args['grade'].strip()} · {status}", "", body, ""]
    if sources:
        lines += ["## Curriculum sources", "", sources, ""]
    lines += ["## Provenance", "",
              f"- Drafted by {args.get('agent') or 'NEXA Teacher Assistant'} under "
              f"{args.get('policy') or 'the NEXA Teaching Intelligence Policy'}; AI-assisted.",
              *([f"- Reviewed and approved by: {reviewed}."] if reviewed else []),
              f"- NEXA run: {args['run_id'].strip()}.",
              f"- Content digest: {digest}.",
              "- The teacher decides how this material is used. NEXA does not mark, rank or make decisions about "
              "students."]
    doc = "\n".join(lines) + "\n"
    return {"document": doc, "digest": digest, "kind": args["kind"], "chars": len(doc),
            "text": f"{kind}, {len(doc)} characters, {digest[:19]}"}


# ---------------------------------------------------------------- curriculum mapping (NEXA-M4-004)

# The Tool Gateway does not import the Knowledge Service; these mirror its
# grade guard (v1/knowledge/curriculum.py) and outcome-code pattern.
_GRADE_NAMED = re.compile(r"\b(?:grade|gr\.?|year|yr\.?)\s*(1[0-2]|[1-9])(?!\d)", re.I)
_OUTCOME = re.compile(r"\b(1[0-2]|[7-9])\.\d{1,2}\.\d{1,2}(?:\.\d{1,2})?[a-z]?\b")
OFFICIAL = ("official_syllabus",)
MAP_TOP_K = 8


def _grades_of(md: dict[str, Any]) -> list[int]:
    g = md.get("grade_levels")
    if isinstance(g, str):
        g = [x for x in re.split(r"[,\s\[\]]+", g) if x]
    out = []
    for x in g or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _codes(md: dict[str, Any], text: str) -> list[str]:
    raw = md.get("outcome_codes") or ""
    codes = [c for c in (raw.split(",") if isinstance(raw, str) else raw) if c]
    for m in _OUTCOME.finditer(text or ""):
        if m.group(0) not in codes:
            codes.append(m.group(0))
    return codes


async def _query(call: Call, text: str, filters: dict[str, Any], top_k: int = MAP_TOP_K) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"query": text[:2000], "top_k": top_k}
    if filters:
        body["filters"] = filters
    status, data = await call.connection.request("POST", "/v1/knowledge/query", json_body=body)
    if status != 200:
        raise HandlerError(502 if status >= 500 else status,
                           f"knowledge service answered {status}: {str(data.get('detail', ''))[:200]}")
    return [r for r in (data.get("results") or []) if isinstance(r, dict)]


def _src(r: dict[str, Any]) -> dict[str, Any]:
    md, p = r.get("metadata") or {}, r.get("provenance") or {}
    return {"title": p.get("title") or md.get("title"), "source_type": md.get("source_type"),
            "subject": md.get("subject"), "grades": _grades_of(md), "version_id": p.get("version_id"),
            "citation": r.get("citation") or p.get("citation"), "score": round(float(r.get("score") or 0), 3)}


_GENERIC_HEADING = re.compile(
    r"^(check yourself|activity|activities|summary|review|exercises?|questions?|introduction|key (terms|ideas|points)"
    r"|learning (outcomes?|objectives?)|answers?|glossary|notes?|contents|references|assessment|lesson \d+|page \d+)\b",
    re.I)
_TITLE_NOISE = re.compile(r"[_\s-]*(lesson[_ ]?notes?|lecture[_ ]?notes?|teacher[_ ]?notes?|student[_ ]?notes?|ppt|"
                          r"presentation|slides?|notes?)\s*$", re.I)
_STOP = set("""about above after again against also among another based because been before being below between
both cannot could does doing down during each from further have having here into itself just like many more most
much must only other over same should some such than that their them then there these they this those through under
until very what when where which while will with within without would your what's explain describe state list
identify discuss students learners able know understand learn learning outcome outcomes grade unit topic""".split())
# Source types that speak for the curriculum carry more weight in a vote than lesson material.
_WEIGHT = {"official_syllabus": 1.6, "teacher_guide": 1.4, "student_guide": 1.4}


def _clean_title(t: Optional[str]) -> Optional[str]:
    if not t:
        return None
    t = re.sub(r"\.(docx?|pptx?|pdf|html?)$", "", t, flags=re.I)
    t = re.sub(r"^s\d+_cm\d+_", "", t)
    t = re.sub(r"^\d+(\.\d+)*[_\s]+", "", t)
    for _ in range(2):
        t = _TITLE_NOISE.sub("", t)
    t = re.sub(r"[_]+", " ", t).strip(" -_")
    return t or None


def _topic(md: dict[str, Any]) -> Optional[str]:
    """The topic a passage belongs to: its curriculum position, else its
    lesson's title, else a section heading that is not a generic one."""
    if md.get("topic_title"):
        return md["topic_title"]
    if md.get("source_type") in ("lms_content", "lesson_notes", "teacher_material"):
        t = _clean_title(md.get("title"))
        if t and not _GENERIC_HEADING.match(t):
            return t
    for sp in reversed([x.strip() for x in str(md.get("section_path") or "").split(">") if x.strip()]):
        h = re.sub(r"^\d+[.)]\s*", "", sp)
        if h and not _GENERIC_HEADING.match(h) and len(h) <= 80:
            return h
    return None


def _unit(md: dict[str, Any]) -> Optional[str]:
    return " ".join(x for x in (md.get("module_code"), md.get("module_title")) if x) or md.get("thema") or None


def _stems(text: str) -> set:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def _weighted(hits: list[dict[str, Any]], key) -> list[tuple[Any, float]]:
    """(value, share of the vote) best first: each passage votes with its score,
    discounted by rank and weighted by authority."""
    w: dict[Any, float] = collections.defaultdict(float)
    for i, h in enumerate(hits):
        md = h.get("metadata") or {}
        v = key(md)
        if v in (None, ""):
            continue
        w[v] += max(float(h.get("score") or 0), 0.01) / (1 + 0.35 * i) * _WEIGHT.get(md.get("source_type"), 1.0)
    total = sum(w.values()) or 1.0
    return sorted(((v, x / total) for v, x in w.items()), key=lambda kv: -kv[1])


def _statement(text: str, code: str) -> str:
    i = (text or "").find(code)
    if i < 0:
        return ""
    rest = text[i + len(code):].lstrip(" :.-–\t)")
    end = min([p for p in (rest.find("\n"), rest.find(". ")) if p >= 0] or [len(rest)])
    return re.sub(r"\s+", " ", rest[:end]).strip()[:240]


@handler("curriculum_map")
async def curriculum_map(args: dict[str, Any], call: Call) -> dict[str, Any]:
    """Where a question or a piece of teaching content sits in the curriculum:
    grade, subject, unit, topic and official learning outcomes, with the
    sources and a confidence. Never maps to another grade than the one asked
    for or named; takes outcomes only from the official syllabus, only of the
    mapped grade, and only those whose statement matches the text."""
    text = args["text"]
    g = str(args.get("grade") or "").strip()
    asked_grade = int(g) if g.isdigit() else None
    named = [int(x) for x in _GRADE_NAMED.findall(text)]
    grade = asked_grade or (named[0] if named else None)
    subject = str(args.get("subject") or "").strip()
    subject = None if subject.lower() in ("", "any") else subject
    filters: dict[str, Any] = {}
    if grade:
        filters["grades"] = [grade]
    if subject:
        filters["subjects"] = [subject]
    hits = await _query(call, text, filters)
    where = (f"Grade {grade} " if grade else "") + (subject or "")
    if not hits:
        msg = (f"NEXA holds no approved {where.strip() or 'curriculum'} material that matches this. "
               "It is not mapped to another grade or subject.")
        return {"status": "not_covered", "grade": grade, "subject": subject, "unit": None, "topic": None,
                "outcomes": [], "also_grades": [], "confidence": "none", "agreement": 0.0, "sources": [],
                "note": msg, "text": msg}
    top = hits[:6]
    if subject:
        subj, s_agree = subject, 1.0
    else:
        votes = _weighted(top, lambda md: md.get("subject"))
        subj, s_agree = votes[0] if votes else (None, 0.0)
        if s_agree < 0.55:
            cands = [v for v, _ in votes[:3]]
            msg = ("This could belong to more than one subject (" + ", ".join(cands) + "). Say which subject "
                   "and grade it is for.")
            return {"status": "ambiguous", "grade": grade, "subject": None, "candidates": cands, "unit": None,
                    "topic": None, "outcomes": [], "also_grades": [], "confidence": "low",
                    "agreement": round(s_agree, 2), "sources": [_src(h) for h in top], "note": msg, "text": msg}
    in_subj = [h for h in hits if (h.get("metadata") or {}).get("subject") == subj] or top
    also: list[int] = []
    if grade is None:
        gv = _weighted(in_subj[:6], lambda md: (_grades_of(md) or [None])[0])
        grade, g_agree = gv[0] if gv else (None, 0.0)
        also = sorted(v for v, share in gv[1:] if share >= 0.25)
    else:
        g_agree = 1.0
    same = [h for h in in_subj if grade is None or grade in _grades_of(h.get("metadata") or {})] or in_subj
    topic = (_weighted(same[:5], _topic) or [(None, 0)])[0][0]
    unit = (_weighted(same[:5], _unit) or [(None, 0)])[0][0]
    # Outcomes come only from the official syllabus (authority rule R3), of the
    # mapped grade, and only those whose own statement shares words with the
    # text or its topic: a syllabus passage lists many outcomes.
    off_filters = {"source_types": list(OFFICIAL), "subjects": [subj]} | ({"grades": [grade]} if grade else {})
    official = await _query(call, text + (f" {topic}" if topic else ""), off_filters, top_k=5)
    want = _stems(text) | _stems(topic or "")
    found: dict[str, dict[str, Any]] = {}
    listed: list[str] = []
    for h in official:
        body = h.get("text") or ""
        for code in _codes(h.get("metadata") or {}, body):
            if grade and not code.startswith(f"{grade}."):
                continue
            if code not in listed:
                listed.append(code)
            st = _statement(body, code)
            overlap = len(_stems(st) & want)
            if st and overlap and (code not in found or overlap > found[code]["overlap"]):
                found[code] = {"code": code, "statement": st, "overlap": overlap, "source": _src(h)["title"],
                               "citation": _src(h)["citation"]}
    outcomes = sorted(found.values(), key=lambda o: -o["overlap"])[:4]
    for o in outcomes:
        o.pop("overlap", None)
    agreement = round(min(s_agree, g_agree), 2)
    notes = []
    if also:
        notes.append("This topic is also taught in Grade " + ", ".join(str(x) for x in also) + "; give the grade "
                     "to map it to one.")
    if not outcomes:
        notes.append(("The official syllabus passages found list " + ", ".join(listed[:6]) + ", but none of these "
                      "outcome statements matches this text; a curriculum reviewer should choose the outcome.")
                     if listed else
                     "No official syllabus outcome was found for this; the mapping rests on lesson material and "
                     "needs a curriculum reviewer's check.")
    if agreement < 0.6:
        notes.append("The sources found do not all agree on the subject or grade; check the mapping.")
    confidence = "high" if outcomes and agreement >= 0.6 and not also else "low"
    lines = [f"Curriculum mapping ({confidence} confidence)",
             f"- Grade: {grade if grade is not None else 'not determined'}",
             f"- Subject: {subj}",
             f"- Unit: {unit or 'not determined'}",
             f"- Topic: {topic or 'not determined'}",
             "- Official learning outcomes: " + (", ".join(o["code"] for o in outcomes) if outcomes else "none found")]
    if also:
        lines.append("- Also covered in: " + ", ".join(f"Grade {x}" for x in also))
    lines += [f"  - {o['code']}: {o['statement']} ({o['source']})" for o in outcomes]
    lines.append("- Sources: " + "; ".join(f"{s['title']} ({s['source_type']})" for s in
                                           (_src(h) for h in same[:3])))
    lines += [f"- Note: {n}" for n in notes]
    return {"status": "mapped", "grade": grade, "subject": subj, "unit": unit, "topic": topic,
            "outcomes": outcomes, "also_grades": also, "confidence": confidence, "agreement": agreement,
            "sources": [_src(h) for h in same[:5]], "official_sources": [_src(h) for h in official[:3]],
            "note": " ".join(notes) or None, "text": "\n".join(lines)}
