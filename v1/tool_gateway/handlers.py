"""Tool implementations behind the contracts (NEXA-M3-004).

builtin handlers run inside the gateway; http handlers use a connector. A
handler receives the validated arguments and a Call (the user, the scopes
granted, a connection), and returns a JSON-safe result with a short "text".
MCP tools need no handler: the gateway calls the bound MCP tool.
"""
from __future__ import annotations

import ast
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
          "student_support": "Student support suggestions"}
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


def _place(md: dict[str, Any]) -> dict[str, Optional[str]]:
    unit = " ".join(x for x in (md.get("module_code"), md.get("module_title")) if x) or md.get("thema") or None
    topic = md.get("topic_title") or None
    if not topic:
        sp = [s.strip() for s in str(md.get("section_path") or "").split(">") if s.strip()]
        topic = sp[-1] if sp else (md.get("title") if md.get("source_type") == "lms_content" else None)
    return {"unit": unit, "topic": topic}


def _vote(values: list[Any]) -> tuple[Any, float]:
    vs = [v for v in values if v not in (None, "")]
    if not vs:
        return None, 0.0
    best = max(set(vs), key=lambda v: (vs.count(v), -vs.index(v)))
    return best, vs.count(best) / len(vs)


@handler("curriculum_map")
async def curriculum_map(args: dict[str, Any], call: Call) -> dict[str, Any]:
    """Where a question or a piece of teaching content sits in the curriculum:
    grade, subject, unit, topic and official learning outcomes, with the
    sources and a confidence. Never maps to another grade than the one asked
    for or named, and takes outcomes only from the official syllabus."""
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
                "outcomes": [], "confidence": "none", "agreement": 0.0, "sources": [], "note": msg, "text": msg}
    top = hits[:5]
    mds = [h.get("metadata") or {} for h in top]
    subj, s_agree = (subject, 1.0) if subject else _vote([m.get("subject") for m in mds])
    if grade is None:
        g_vals = [(_grades_of(m) or [None])[0] for m in mds if m.get("subject") == subj]
        grade, g_agree = _vote(g_vals)
    else:
        g_agree = 1.0
    if not subject and s_agree < 0.6:
        cands = sorted({m.get("subject") for m in mds if m.get("subject")})
        msg = ("This could belong to more than one subject (" + ", ".join(cands) + "). Say which subject "
               "and grade it is for.")
        return {"status": "ambiguous", "grade": grade, "subject": None, "candidates": cands, "unit": None,
                "topic": None, "outcomes": [], "confidence": "low", "agreement": round(s_agree, 2),
                "sources": [_src(h) for h in top], "note": msg, "text": msg}
    same = [h for h in hits if (h.get("metadata") or {}).get("subject") == subj
            and (grade is None or grade in _grades_of(h.get("metadata") or {}))] or top
    places = [_place(h.get("metadata") or {}) for h in same[:5]]
    unit, _ = _vote([p["unit"] for p in places])
    topic, _ = _vote([p["topic"] for p in places])
    # Outcomes come only from the official syllabus (authority rule R3).
    off_filters = {"source_types": list(OFFICIAL), "subjects": [subj]} | ({"grades": [grade]} if grade else {})
    official = await _query(call, text, off_filters, top_k=5)
    outcomes: list[dict[str, Any]] = []
    for h in official:
        md = h.get("metadata") or {}
        for code in _codes(md, h.get("text") or ""):
            if grade and not code.startswith(f"{grade}."):
                continue
            if code not in [o["code"] for o in outcomes]:
                outcomes.append({"code": code, "source": _src(h)["title"], "citation": _src(h)["citation"]})
    outcomes = outcomes[:6]
    agreement = round(min(s_agree, g_agree), 2)
    confidence = "high" if outcomes and agreement >= 0.8 else "low"
    note = None
    if not outcomes:
        note = ("No official syllabus outcome was found for this; the mapping rests on lesson material and "
                "needs a curriculum reviewer's check.")
    elif agreement < 0.8:
        note = "The sources found do not all agree on the subject or grade; check the mapping."
    lines = [f"Curriculum mapping ({confidence} confidence)",
             f"- Grade: {grade if grade is not None else 'not determined'}",
             f"- Subject: {subj}",
             f"- Unit: {unit or 'not determined'}",
             f"- Topic: {topic or 'not determined'}",
             "- Official learning outcomes: " + (", ".join(o["code"] for o in outcomes) if outcomes else "none found")]
    lines.append("- Sources: " + "; ".join(f"{s['title']} ({s['source_type']})" for s in
                                           (_src(h) for h in same[:3])))
    if note:
        lines.append(f"- Note: {note}")
    return {"status": "mapped", "grade": grade, "subject": subj, "unit": unit, "topic": topic,
            "outcomes": outcomes, "confidence": confidence, "agreement": agreement,
            "sources": [_src(h) for h in same[:5]], "official_sources": [_src(h) for h in official[:3]],
            "note": note, "text": "\n".join(lines)}
