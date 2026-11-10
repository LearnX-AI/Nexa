"""Source provenance, citations and the grounding contract (NEXA-M2-006).

Every answer NEXA grounds in curriculum knowledge follows one contract,
versioned as CITATION_SCHEMA_VERSION and documented in
docs/knowledge/NEXA-M2-006-provenance-citations-grounding.md:

1. Provenance. Every retrieved passage carries document (source_id), source
   version (version_id, content_checksum), chunk (chunk_id, run_id), location
   (page, printed page label, section path, in-document anchor) and the index
   and embedding model that found it. These come from the stored metadata of
   the passage itself, so reranking and context construction cannot detach a
   passage from its origin.
2. Evidence. Before any generation the retrieved passages are graded strong,
   weak or none, and checked for an authority conflict (NEXA-M2-005).
3. Behaviour by evidence:
   - none: no model call. A fixed fallback says the approved material does not
     cover the question. NEXA does not answer from general knowledge here.
   - weak: the model may answer only from the passages and the answer is
     marked low confidence, with a fixed caution line.
   - conflict: authoritative passages lead the context and lower-authority
     ones are labelled supplementary; the answer carries a note.
   - strong: a normal grounded answer.
4. Citations. The model may cite only the markers [S1]..[Sn] it was given.
   Any other marker, or a numbered reference like [3], is removed and
   reported as fabricated. An answer with no valid citation is withheld
   (status ungrounded). Each cited sentence is checked for word overlap with
   the passages it cites, and weakly supported sentences are reported.
5. Traceability. A citation resolves through GET /v1/knowledge/chunks/{id}
   to the chunk, its source version and the original file by checksum.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from . import config
from .retrieval import RetrievalResult, _terms

CITATION_SCHEMA_VERSION = "1.0.0"

Status = Literal["grounded", "grounded_weak", "no_evidence", "ungrounded", "generation_unavailable"]


class Provenance(BaseModel):
    source_id: Optional[str] = None
    version_id: Optional[str] = None
    content_checksum: Optional[str] = None
    chunk_id: str
    run_id: Optional[str] = None
    title: Optional[str] = None
    source_type: Optional[str] = None
    authority_tier: Optional[int] = None
    scope: Optional[str] = None
    school_id: Optional[str] = None
    language: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    page_label: Optional[str] = None
    page_basis: Optional[str] = None
    section_path: list[str] = Field(default_factory=list)
    anchor: Optional[str] = None
    citation: Optional[str] = Field(None, description="Human-readable reference, e.g. 'Biology Syllabus, p. 12'")
    source_uri: Optional[str] = None
    lms: Optional[dict[str, Any]] = None
    index_id: Optional[str] = None
    embedding: Optional[str] = Field(None, description="Fingerprint of the embedding model of the index")
    chunker_version: Optional[str] = None


class Citation(BaseModel):
    marker: str = Field(..., description="S1, S2 ... as used in the answer text")
    rank: int
    used: bool = Field(..., description="True when the answer cites this marker")
    authoritative: bool = True
    quote: str = Field(..., description="Opening of the passage, verbatim")
    support: Optional[float] = Field(None, description="Word overlap of the sentences citing it with the passage")
    provenance: Provenance


class Evidence(BaseModel):
    level: Literal["strong", "weak", "none"]
    conflict: bool = False
    top_similarity: Optional[float] = None
    top_coverage: Optional[float] = None
    passages: int = 0
    thresholds: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class Integrity(BaseModel):
    valid_markers: list[str] = Field(default_factory=list)
    fabricated_markers: list[str] = Field(default_factory=list)
    uncited_sentences: int = 0
    weakly_supported_sentences: int = 0


class GroundedAnswer(BaseModel):
    schema_version: str = CITATION_SCHEMA_VERSION
    status: Status
    answer: Optional[str]
    confidence: Literal["high", "low", "none"]
    notes: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    evidence: Evidence
    integrity: Integrity = Field(default_factory=Integrity)
    model: Optional[str] = None


def citation_json_schema() -> dict[str, Any]:
    s = GroundedAnswer.model_json_schema()
    s["$id"] = f"https://nexa.learnx.ai/schemas/grounded-answer/{CITATION_SCHEMA_VERSION}.json"
    s["title"] = f"NEXA Grounded Answer {CITATION_SCHEMA_VERSION}"
    return s


# ---------------------------------------------------------------- provenance

def provenance_of(item: dict[str, Any], index_id: Optional[str] = None,
                  embedding: Optional[str] = None) -> Provenance:
    md = item.get("metadata") or {}

    def num(k):
        try:
            return int(md[k]) if md.get(k) is not None else None
        except (TypeError, ValueError):
            return None

    lms = None
    if md.get("lms_item_id"):
        lms = {k[4:]: md.get(k) for k in ("lms_system", "lms_site_id", "lms_course_id", "lms_item_id",
                                          "lms_revision", "lms_url") if md.get(k)}
    sp = md.get("section_path") or ""
    return Provenance(
        source_id=md.get("source_id"), version_id=md.get("version_id"), content_checksum=md.get("content_checksum"),
        chunk_id=item["chunk_id"], run_id=md.get("run_id"), title=md.get("title"), source_type=md.get("source_type"),
        authority_tier=num("authority_tier"), scope=md.get("scope"), school_id=md.get("school_id"),
        language=md.get("language"), page_start=num("page_start"), page_end=num("page_end"),
        page_label=md.get("page_label"), page_basis=md.get("page_basis"),
        section_path=[p for p in sp.split(" > ") if p] if isinstance(sp, str) else list(sp),
        anchor=md.get("anchor"), citation=md.get("citation"), source_uri=md.get("source_uri"), lms=lms,
        index_id=index_id, embedding=embedding, chunker_version=md.get("chunker_version"))


# ---------------------------------------------------------------- evidence

def thresholds_for(embedding_fingerprint: Optional[str]) -> dict[str, float]:
    """Similarity thresholds depend on the embedding model: cosine scales
    differ between models. Calibrated on the gold set (answerable and
    unanswerable questions) by v1/tools/knowledge_retrieval_bench.py."""
    scheme = (embedding_fingerprint or "").split("|")[-1]
    t = dict(config.EVIDENCE_THRESHOLDS.get(scheme) or config.EVIDENCE_THRESHOLDS["default"])
    t["coverage_strong"] = config.EVIDENCE_COVERAGE_STRONG
    t["coverage_min"] = config.EVIDENCE_COVERAGE_MIN
    return t


def assess(result: RetrievalResult) -> Evidence:
    rows = result.results
    th = thresholds_for(result.index.fingerprint if result.index else None)
    if not rows:
        return Evidence(level="none", passages=0, thresholds=th,
                        reasons=["no approved passage matched the question and filters"])
    top = rows[0]
    sim = top.get("scores", {}).get("similarity")
    cov = (top.get("coverage") or {}).get("coverage", 0.0)
    conflict = bool(result.explain.get("conflicts"))
    reasons = []
    if sim is None:  # lexical-only retrieval: judge on coverage alone
        level = "strong" if cov >= th["coverage_strong"] else ("weak" if cov >= th["coverage_min"] else "none")
        reasons.append(f"lexical only: top passage covers {cov:.0%} of the question's words")
    elif sim >= th["strong"] or (sim >= th["min"] and cov >= th["coverage_strong"]):
        level = "strong"
        reasons.append(f"top similarity {sim:.3f}, word coverage {cov:.0%}")
    elif sim >= th["min"] or cov >= th["coverage_strong"]:
        level = "weak"
        reasons.append(f"top similarity {sim:.3f} below {th['strong']}, word coverage {cov:.0%}")
    else:
        level = "none"
        reasons.append(f"top similarity {sim:.3f} below {th['min']} and word coverage {cov:.0%} "
                       f"below {th['coverage_strong']:.0%}")
    if conflict:
        reasons.append("an official source and a lower-authority source cover the same point; "
                       "the official one leads")
    return Evidence(level=level, conflict=conflict, top_similarity=sim, top_coverage=cov,
                    passages=len(rows), thresholds=th, reasons=reasons)


# ---------------------------------------------------------------- context

def build_context(result: RetrievalResult, max_chars: Optional[int] = None
                  ) -> tuple[str, list[dict[str, Any]]]:
    """Numbered context blocks [S1]..[Sn] in rank order, each headed with its
    reference and authority, and the marker -> passage map. Passages are cut,
    never merged, so each marker stays one chunk with one provenance."""
    max_chars = max_chars or config.ANSWER_CONTEXT_CHARS
    blocks, used, total = [], [], 0
    for i, r in enumerate(result.results, 1):
        auth = r.get("authority") or {}
        role = "authoritative" if auth.get("authoritative", True) else "supplementary"
        md = r.get("metadata") or {}
        head = f"[S{i}] {md.get('citation') or md.get('title') or r['chunk_id']} ({md.get('source_type', 'source')}, {role})"
        body = " ".join((r.get("text") or "").split())
        room = max_chars - total - len(head) - 2
        if room < 200 and blocks:
            break
        body = body[:max(room, 200)]
        blocks.append(head + "\n" + body)
        total += len(head) + len(body) + 2
        used.append({"marker": f"S{i}", "item": r, "authoritative": role == "authoritative"})
    return "\n\n".join(blocks), used


SYSTEM_PROMPT = (
    "You are NEXA, a tutor for Papua New Guinea secondary school students. Answer the question using ONLY the "
    "numbered sources provided. After every sentence that uses a source, cite it with its marker in square "
    "brackets, for example [S1] or [S1][S2]. Use only the markers given; never invent a source, page or "
    "reference. Sources marked authoritative come from the official curriculum; supplementary sources may add "
    "explanation but must not be used to contradict an authoritative source. If the sources do not contain "
    "the answer, reply with exactly: INSUFFICIENT_EVIDENCE"
)
AUDIENCE = {
    "student": "Write for a student: clear, simple and step by step.",
    "teacher": "Write for a teacher: precise, and note curriculum references where relevant.",
}

FALLBACK_NO_EVIDENCE = (
    "I could not find this in the approved curriculum materials available to you, so I will not guess. "
    "Try rephrasing the question, naming the subject or grade, or ask your teacher."
)
FALLBACK_UNGROUNDED = (
    "I found related curriculum material but could not produce an answer that is properly supported by it. "
    "Please read the sources listed below, or ask your teacher."
)
FALLBACK_GENERATION = (
    "The answer service is unavailable right now. These are the curriculum passages that match your question."
)
NOTE_WEAK = "The approved material only partly covers this question, so treat this answer with care."
NOTE_CONFLICT = ("Different sources cover this point. This answer follows the official curriculum; "
                 "school or LMS material is used only to add explanation.")


# ---------------------------------------------------------------- validation

_MARKER = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]", re.I)
_NUMERIC_REF = re.compile(r"\[\s*\d+(?:\s*[,;-]\s*\d+)*\s*\]")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")


def validate(answer: str, used: list[dict[str, Any]]) -> tuple[str, Integrity, dict[str, float]]:
    """Strip markers that do not refer to a given passage, and measure how well
    each cited sentence is supported by the passages it cites."""
    valid = {u["marker"].upper(): u for u in used}
    fabricated: list[str] = []

    def fix(m: re.Match) -> str:
        keep = []
        for raw in re.split(r"\s*[,;]\s*", m.group(1)):
            mk = raw.strip().upper()
            if mk in valid:
                keep.append(mk)
            else:
                fabricated.append(mk)
        return "".join(f"[{k}]" for k in keep)

    text = _MARKER.sub(fix, answer)
    for m in _NUMERIC_REF.findall(text):
        fabricated.append(m.strip("[] "))
    text = _NUMERIC_REF.sub("", text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()

    sentences = [s for s in _SENT.split(text) if s.strip()]
    uncited = weak = 0
    support: dict[str, list[float]] = {}
    for s in sentences:
        marks = [m.upper() for m in re.findall(r"\[(S\d+)\]", s, re.I)]
        content = [t for t in _terms(_MARKER.sub("", s))]
        if not marks:
            if len(content) >= 4:  # a heading or a short connective needs no citation
                uncited += 1
            continue
        passage_terms: set[str] = set()
        for mk in marks:
            passage_terms |= set(_terms(valid[mk]["item"].get("text") or ""))
        overlap = (sum(t in passage_terms for t in content) / len(content)) if content else 1.0
        if overlap < config.CITATION_MIN_SUPPORT:
            weak += 1
        for mk in marks:
            support.setdefault(mk, []).append(overlap)
    found = sorted({m.upper() for m in re.findall(r"\[(S\d+)\]", text, re.I)}, key=lambda x: int(x[1:]))
    integ = Integrity(valid_markers=found, fabricated_markers=sorted(set(fabricated)),
                      uncited_sentences=uncited, weakly_supported_sentences=weak)
    return text, integ, {k: round(sum(v) / len(v), 3) for k, v in support.items()}


# ---------------------------------------------------------------- generation

async def _chat(messages: list[dict[str, str]], trace_id: Optional[str]) -> tuple[str, str]:
    if not config.GATEWAY_TOKEN:
        raise RuntimeError("NEXA_KNOWLEDGE_GATEWAY_TOKEN not set")
    headers = {"Authorization": f"Bearer {config.GATEWAY_TOKEN}"}
    if trace_id:
        headers["X-NEXA-Trace-Id"] = trace_id
    async with httpx.AsyncClient(timeout=config.ANSWER_TIMEOUT) as client:
        resp = await client.post(f"{config.GATEWAY_URL}/v1/chat/completions", headers=headers, json={
            "model": config.ANSWER_MODEL, "messages": messages, "temperature": config.ANSWER_TEMPERATURE,
            "max_tokens": config.ANSWER_MAX_TOKENS})
    if resp.status_code != 200:
        raise RuntimeError(f"gateway {resp.status_code}")
    data = resp.json()
    return data["choices"][0]["message"]["content"], data.get("model") or config.ANSWER_MODEL


# Replaced in tests.
chat = _chat


def _citations(used: list[dict[str, Any]], result: RetrievalResult, integ: Integrity,
               support: dict[str, float]) -> list[Citation]:
    idx = result.index.index_id if result.index else None
    fp = result.index.fingerprint if result.index else None
    out = []
    for u in used:
        it = u["item"]
        quote = " ".join((it.get("text") or "").split())[:240]
        out.append(Citation(marker=u["marker"], rank=it.get("rank", 0), used=u["marker"] in integ.valid_markers,
                            authoritative=u["authoritative"], quote=quote, support=support.get(u["marker"]),
                            provenance=provenance_of(it, idx, fp)))
    return out


async def answer(question: str, result: RetrievalResult, audience: str = "student",
                 trace_id: Optional[str] = None) -> GroundedAnswer:
    ev = assess(result)
    if ev.level == "none":
        return GroundedAnswer(status="no_evidence", answer=FALLBACK_NO_EVIDENCE, confidence="none",
                              evidence=ev, notes=["no model was called"])
    context, used = build_context(result)
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT + " " + AUDIENCE.get(audience, AUDIENCE["student"])},
        {"role": "user", "content": f"Sources:\n{context}\n\nQuestion: {question}"},
    ]
    try:
        raw, model = await chat(msgs, trace_id)
    except Exception:  # noqa: BLE001 - the passages are still useful without generation
        cites = _citations(used, result, Integrity(), {})
        return GroundedAnswer(status="generation_unavailable", answer=FALLBACK_GENERATION, confidence="none",
                              evidence=ev, citations=cites)
    if "INSUFFICIENT_EVIDENCE" in raw.upper().replace(" ", "_"):
        ev.reasons.append("the model found that the passages do not answer the question")
        return GroundedAnswer(status="no_evidence", answer=FALLBACK_NO_EVIDENCE, confidence="none", evidence=ev,
                              model=model, citations=_citations(used, result, Integrity(), {}))
    text, integ, support = validate(raw, used)
    cites = _citations(used, result, integ, support)
    if not integ.valid_markers:
        return GroundedAnswer(status="ungrounded", answer=FALLBACK_UNGROUNDED, confidence="none", evidence=ev,
                              integrity=integ, citations=cites, model=model,
                              notes=["the generated answer cited none of the passages and was withheld"])
    notes = []
    if ev.level == "weak":
        notes.append(NOTE_WEAK)
    if ev.conflict:
        notes.append(NOTE_CONFLICT)
    if integ.fabricated_markers:
        notes.append(f"removed {len(integ.fabricated_markers)} reference(s) that did not match a source")
    weak = ev.level == "weak" or integ.weakly_supported_sentences > 0
    return GroundedAnswer(status="grounded_weak" if ev.level == "weak" else "grounded", answer=text,
                          confidence="low" if weak else "high", notes=notes, citations=cites, evidence=ev,
                          integrity=integ, model=model)
