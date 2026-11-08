"""Structure-aware semantic chunking (NEXA-M2-003).

Strategy "nexa-structural", version CHUNKER_VERSION.

1. Blocks are walked in reading order with a heading stack, so every block
   knows its section path and curriculum position (thema, module, topic,
   outcome codes).
2. Blocks are grouped into atomic units that must not be cut: a table (split
   by rows with the header repeated only if it alone exceeds the limit), an
   exercise with its answer space, a worked example, an equation with the line
   that introduces it, a lead-in sentence ending in ":" with the list it
   introduces.
3. Units are packed into chunks up to target_tokens within one section. A topic
   level heading (level <= hard_boundary_level) always starts a new chunk. A
   chunk is closed early only at a unit boundary, never inside a unit.
4. A paragraph longer than max_tokens is split on sentence boundaries.
5. When one section spans several chunks, each continuation chunk starts with
   about overlap_tokens of the previous chunk's trailing sentences. There is no
   overlap across sections: the heading context carries continuity instead.
6. Each chunk records its strategy, version and config hash, parent document
   version, pages, section path, anchors and its neighbours.

Token counts use a dependency-free estimate (words plus punctuation pieces),
which tracks BPE token counts for English curriculum prose within about 10%.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..curriculum import detect_heading, outcome_codes
from ..ingestion.model import Block, BlockKind

CHUNKER_VERSION = "1.0.0"
STRATEGY = "nexa-structural"

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|vs|Dr|Mr|Mrs|Prof|Fig|Eq|approx|No)\.$")


def count_tokens(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def split_sentences(text: str) -> list[str]:
    parts = _SENT_SPLIT.split(text.strip())
    out: list[str] = []
    for p in parts:
        if out and _ABBREV.search(out[-1]):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return [p for p in out if p]


class ChunkingConfig(BaseModel):
    """Versioned, hashable chunking parameters. Changing any field changes the
    config hash, which changes chunk ids, so old and new chunk sets never mix."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_tokens: int = Field(320, ge=64, le=2048)
    max_tokens: int = Field(512, ge=96, le=4096)
    min_tokens: int = Field(60, ge=0, le=512)
    overlap_tokens: int = Field(48, ge=0, le=512)
    hard_boundary_level: int = Field(2, ge=1, le=6, description="Document headings at or above this level always cut; curriculum THEMA, MODULE and Topic headings always cut")
    atomic_overflow: float = Field(1.6, ge=1.0, le=4.0,
                                   description="An atomic unit may exceed max_tokens by this factor before it is split")
    include_context_header: bool = True
    subject_profile: str = "default"

    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


@dataclass
class SubjectProfile:
    name: str
    aggressive_equations: bool = False
    keep_worked_examples: bool = True
    atomic_overflow: Optional[float] = None


PROFILES = {
    "default": SubjectProfile("default"),
    "mathematics": SubjectProfile("mathematics", aggressive_equations=True, atomic_overflow=2.0),
    "physics": SubjectProfile("physics", aggressive_equations=True, atomic_overflow=2.0),
    "chemistry": SubjectProfile("chemistry", aggressive_equations=True, atomic_overflow=1.8),
    "biology": SubjectProfile("biology"),
}


def profile_for_subject(subject: Optional[str]) -> str:
    s = (subject or "").lower()
    if s in ("mathematics", "physics", "chemistry", "biology"):
        return s
    return "default"


@dataclass
class PositionedBlock:
    block: Block
    page: int
    page_label: Optional[str]
    confidence: Optional[float] = None
    method: str = "native"


@dataclass
class _Unit:
    blocks: list[PositionedBlock]
    atomic: bool
    tokens: int
    section_path: tuple[str, ...]
    curriculum: dict[str, Any]

    @property
    def text(self) -> str:
        return "\n".join(_render(pb.block) for pb in self.blocks)


def _render(b: Block) -> str:
    if b.kind == BlockKind.LIST_ITEM:
        return f"- {b.text}" if not b.text.startswith(("-", "•")) else b.text
    if b.kind == BlockKind.TABLE and b.rows:
        return "\n".join(" | ".join(r) for r in b.rows)
    if b.kind == BlockKind.NOTES:
        return f"Teacher notes: {b.text}"
    return b.text


@dataclass
class Chunk:
    ordinal: int
    text: str
    embedding_text: str
    tokens: int
    section_path: list[str]
    curriculum: dict[str, Any]
    page_start: Optional[int]
    page_end: Optional[int]
    page_label: Optional[str]
    anchors: list[str]
    block_kinds: list[str]
    methods: list[str]
    min_ocr_confidence: Optional[float]
    overlap_prefix_chars: int = 0
    chunk_id: str = ""
    prev_chunk_id: Optional[str] = None
    next_chunk_id: Optional[str] = None
    section_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _section_key(path: tuple[str, ...]) -> str:
    return hashlib.sha256("␟".join(path).encode()).hexdigest()[:12]


def _units(pblocks: list[PositionedBlock], cfg: ChunkingConfig, prof: SubjectProfile,
           base_curriculum: dict[str, Any]) -> list[_Unit]:
    """Walk blocks with a heading stack and group them into atomic units."""
    stack: list[tuple[int, str, dict[str, Any]]] = []
    units: list[_Unit] = []
    i = 0
    overflow = prof.atomic_overflow or cfg.atomic_overflow

    def ctx() -> tuple[tuple[str, ...], dict[str, Any]]:
        cur = dict(base_curriculum)
        for _, _, fields in stack:
            cur.update({k: v for k, v in fields.items() if v is not None})
        return tuple(t for _, t, _ in stack), cur

    def new_unit(blocks: list[PositionedBlock], atomic: bool) -> None:
        path, cur = ctx()
        units.append(_Unit(blocks, atomic, sum(count_tokens(_render(b.block)) for b in blocks), path, cur))

    while i < len(pblocks):
        pb = pblocks[i]
        b = pb.block
        if b.kind == BlockKind.HEADING:
            level, title, fields, _ = heading_info(b)
            fields = dict(fields)
            if fields.get("topic_num") and stack:
                mod = next((f.get("module_code") for _, _, f in reversed(stack) if f.get("module_code")), None)
                if mod:
                    fields["topic_code"] = f"{mod}.{fields['topic_num']}"
            fields.pop("topic_num", None)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title, fields))
            # A heading is emitted as a marker unit so the packer can cut here.
            path, cur = ctx()
            units.append(_Unit([pb], True, 0, path, cur))
            i += 1
            continue

        is_example = prof.keep_worked_examples and stack and re.search(
            r"worked example|example\s*\d|sample problem", stack[-1][1], re.I)
        if b.kind == BlockKind.TABLE:
            new_unit([pb], True)
            i += 1
        elif b.kind == BlockKind.EXERCISE:
            group = [pb]
            i += 1
            while i < len(pblocks) and pblocks[i].block.kind == BlockKind.LIST_ITEM:
                group.append(pblocks[i])  # answer options belong to their question
                i += 1
            new_unit(group, True)
        elif b.kind in (BlockKind.EQUATION,) or (
                b.text.rstrip().endswith(":") and i + 1 < len(pblocks)
                and pblocks[i + 1].block.kind in (BlockKind.LIST_ITEM, BlockKind.EQUATION, BlockKind.TABLE)):
            group = [pb]
            i += 1
            while i < len(pblocks) and pblocks[i].block.kind in (BlockKind.LIST_ITEM, BlockKind.EQUATION):
                group.append(pblocks[i])
                i += 1
            if b.kind == BlockKind.EQUATION and units and not units[-1].atomic and units[-1].tokens < cfg.target_tokens:
                # Pull the explanatory sentence before an equation into the unit.
                prev = units.pop()
                group = prev.blocks + group
            new_unit(group, True)
        elif is_example:
            group = [pb]
            i += 1
            while i < len(pblocks) and pblocks[i].block.kind != BlockKind.HEADING:
                group.append(pblocks[i])
                i += 1
            new_unit(group, True)
        else:
            new_unit([pb], False)
            i += 1

    # Split units that are too large.
    out: list[_Unit] = []
    for u in units:
        limit = cfg.max_tokens * (overflow if u.atomic else 1.0)
        if u.tokens <= limit:
            out.append(u)
            continue
        if len(u.blocks) == 1 and u.blocks[0].block.kind == BlockKind.TABLE and u.blocks[0].block.rows:
            out.extend(_split_table(u, cfg))
        elif len(u.blocks) > 1:
            for pb in u.blocks:
                t = count_tokens(_render(pb.block))
                part = _Unit([pb], False, t, u.section_path, u.curriculum)
                out.extend(_split_paragraph(part, cfg) if t > cfg.max_tokens else [part])
        else:
            out.extend(_split_paragraph(u, cfg))
    return out


def _split_table(u: _Unit, cfg: ChunkingConfig) -> list[_Unit]:
    pb = u.blocks[0]
    rows = pb.block.rows or []
    header, body = rows[0], rows[1:]
    parts: list[_Unit] = []
    cur: list[list[str]] = []
    for r in body:
        trial = [header] + cur + [r]
        if cur and count_tokens("\n".join(" | ".join(x) for x in trial)) > cfg.max_tokens:
            parts.append(cur)
            cur = []
        cur.append(r)
    if cur:
        parts.append(cur)
    res = []
    for k, chunk_rows in enumerate(parts):
        rows_k = [header] + chunk_rows
        blk = Block(BlockKind.TABLE, "\n".join(" | ".join(r) for r in rows_k), rows=rows_k,
                    anchor=f"{pb.block.anchor or 'table'}#rows{k + 1}")
        npb = PositionedBlock(blk, pb.page, pb.page_label, pb.confidence, pb.method)
        res.append(_Unit([npb], True, count_tokens(blk.text), u.section_path, u.curriculum))
    return res


def _split_paragraph(u: _Unit, cfg: ChunkingConfig) -> list[_Unit]:
    pb = u.blocks[0]
    sents = split_sentences(pb.block.text)
    pieces: list[list[str]] = [[]]
    for s in sents:
        if pieces[-1] and count_tokens(" ".join(pieces[-1] + [s])) > cfg.target_tokens:
            pieces.append([])
        pieces[-1].append(s)
    res = []
    for k, p in enumerate(pieces):
        text = " ".join(p)
        # A single sentence longer than max_tokens is cut on word boundaries.
        while count_tokens(text) > cfg.max_tokens:
            words = text.split()
            head, text = " ".join(words[: cfg.target_tokens // 2]), " ".join(words[cfg.target_tokens // 2:])
            blk = Block(pb.block.kind, head, anchor=pb.block.anchor)
            res.append(_Unit([PositionedBlock(blk, pb.page, pb.page_label, pb.confidence, pb.method)],
                             False, count_tokens(head), u.section_path, u.curriculum))
        blk = Block(pb.block.kind, text, level=pb.block.level, anchor=pb.block.anchor)
        res.append(_Unit([PositionedBlock(blk, pb.page, pb.page_label, pb.confidence, pb.method)],
                         False, count_tokens(text), u.section_path, u.curriculum))
    return res


def heading_info(b: Block) -> tuple[int, str, dict[str, Any], bool]:
    """(level, title, curriculum fields, is_curriculum_heading) for a heading
    block. Structured sources (DOCX, PPTX, HTML) carry the author's own heading
    text, so it is recognised here; flat text was recognised by the structurer,
    which stored the result on the block."""
    if b.meta and b.meta.get("curriculum_heading"):
        return b.level or 3, b.text, b.meta.get("curriculum") or {}, True
    h = detect_heading(b.text) if not (b.meta and "curriculum_heading" in b.meta) else None
    if h:
        b.meta = {"curriculum_heading": True, "curriculum": h[2]}
        return h[0], h[1], h[2], True
    return b.level or 3, b.text, {}, False


def _is_heading_unit(u: _Unit) -> bool:
    return len(u.blocks) == 1 and u.blocks[0].block.kind == BlockKind.HEADING


def _heading_marker(g: list[_Unit]) -> Optional[_Unit]:
    """When two soft-separated groups merge, re-insert the second group's own
    subsection title so the merged text still shows where the section changed."""
    first = next((u for u in g if not _is_heading_unit(u)), None)
    if not first or not first.section_path:
        return None
    title = first.section_path[-1]
    blk = Block(BlockKind.HEADING, title, level=4)
    return _Unit([PositionedBlock(blk, first.blocks[0].page, first.blocks[0].page_label)], True,
                 count_tokens(title), first.section_path, first.curriculum)


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    out: list[str] = []
    for parts in zip(*paths):
        if all(p == parts[0] for p in parts):
            out.append(parts[0])
        else:
            break
    return tuple(out)


def _tail_sentences(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    picked: list[str] = []
    for s in reversed(split_sentences(text.replace("\n", " "))):
        if count_tokens(" ".join([s] + picked)) > budget:
            break
        picked.insert(0, s)
    return " ".join(picked)


def chunk_blocks(
    pblocks: list[PositionedBlock],
    cfg: ChunkingConfig,
    version_id: str,
    context_prefix: str = "",
    base_curriculum: Optional[dict[str, Any]] = None,
) -> list[Chunk]:
    prof = PROFILES.get(cfg.subject_profile, PROFILES["default"])
    units = _units(pblocks, cfg, prof, base_curriculum or {})

    # Packing. A group records why the chunk before it was closed: "hard" (topic
    # boundary, no overlap, no merging across), "soft" (subsection change) or
    # "size" (same section continued, gets sentence overlap).
    groups: list[tuple[list[_Unit], str]] = []
    cur: list[_Unit] = []
    cur_tokens = 0
    reason = "hard"

    def body_tokens(us: list[_Unit]) -> int:
        return sum(u.tokens for u in us if not _is_heading_unit(u))

    def close(next_reason: str):
        nonlocal cur, cur_tokens, reason
        if body_tokens(cur):
            groups.append((cur, reason))
            reason = next_reason
        elif next_reason == "hard":
            reason = "hard"
        cur, cur_tokens = [], 0

    for u in units:
        if _is_heading_unit(u):
            b = u.blocks[0].block
            level, _, _, curricular = heading_info(b)
            hard = (curricular and level <= 3) or level <= cfg.hard_boundary_level
            if hard:
                close("hard")
            elif cur_tokens >= cfg.target_tokens * 0.5:
                close("soft")
            elif body_tokens(cur):
                # Keep the subsection title inline so the chunk reads correctly.
                hu = _Unit(u.blocks, True, count_tokens(b.text), u.section_path, u.curriculum)
                cur.append(hu)
                cur_tokens += hu.tokens
            continue
        if cur and ((cur_tokens + u.tokens > cfg.target_tokens and cur_tokens >= cfg.min_tokens)
                    or cur_tokens + u.tokens > cfg.max_tokens):
            close("size")
        cur.append(u)
        cur_tokens += u.tokens
    close("hard")

    # Merge chunks that are too small into a neighbour inside the same topic.
    merged: list[tuple[list[_Unit], str]] = []
    for g, why in groups:
        if merged and why != "hard" and (body_tokens(g) < cfg.min_tokens or body_tokens(merged[-1][0]) < cfg.min_tokens) \
                and body_tokens(merged[-1][0]) + body_tokens(g) <= cfg.max_tokens:
            prev_units, prev_why = merged[-1]
            extra = [] if why == "size" else [_heading_marker(g)]
            merged[-1] = (prev_units + [x for x in extra if x] + g, prev_why)
        else:
            merged.append((g, why))

    # Fragments below half of min_tokens (cover lines, sign-offs, a stray
    # heading-like line) are folded into a neighbour that sits at the same
    # curriculum position, even across a hard boundary: on their own they are
    # unretrievable noise, and merging them never mixes two topics.
    def position(g: list[_Unit]) -> tuple:
        cur = g[-1].curriculum
        top = next((u.section_path[:1] for u in g if u.section_path), ())
        return (cur.get("thema"), cur.get("module_code"), cur.get("topic_title"), top)

    tiny = max(cfg.min_tokens // 2, 1)
    folded: list[tuple[list[_Unit], str]] = []
    pending: Optional[tuple[list[_Unit], str]] = None
    for g, why in merged:
        if pending is not None:
            if position(pending[0]) == position(g) and body_tokens(pending[0]) + body_tokens(g) <= cfg.max_tokens:
                g, why = pending[0] + [x for x in [_heading_marker(g)] if x] + g, pending[1]
            else:
                folded.append(pending)
            pending = None
        if body_tokens(g) < tiny:
            if folded and position(folded[-1][0]) == position(g) \
                    and body_tokens(folded[-1][0]) + body_tokens(g) <= cfg.max_tokens:
                folded[-1] = (folded[-1][0] + [x for x in [_heading_marker(g)] if x] + g, folded[-1][1])
            else:
                pending = (g, why)  # try the next chunk instead
            continue
        folded.append((g, why))
    if pending is not None:
        folded.append(pending)
    merged = folded

    chash = cfg.config_hash()
    chunks: list[Chunk] = []
    prev_body = ""
    for ordinal, (g, why) in enumerate(merged):
        section = _common_prefix([u.section_path for u in g if not _is_heading_unit(u)] or [g[0].section_path])
        body = "\n".join(u.text for u in g).strip()
        overlap = ""
        if cfg.overlap_tokens and why == "size" and prev_body:
            overlap = _tail_sentences(prev_body, cfg.overlap_tokens)
        text = (overlap + "\n" + body).strip() if overlap else body
        pbs = [pb for u in g for pb in u.blocks]
        cur_meta: dict[str, Any] = {}
        for u in g:
            cur_meta.update({k: v for k, v in u.curriculum.items() if v is not None})
        codes = outcome_codes(body)
        if codes:
            cur_meta["outcome_codes"] = codes
            topic = cur_meta.get("topic_code")
            if topic and not any(c.startswith(topic) for c in codes):
                # The heading numbering and the outcome codes disagree in the
                # source itself. Keep both and flag it for the content team.
                cur_meta["code_mismatch"] = True
        pages = [pb.page for pb in pbs if pb.page]
        labels = [pb.page_label for pb in pbs if pb.page_label]
        confs = [pb.confidence for pb in pbs if pb.confidence is not None]
        header = ""
        if cfg.include_context_header:
            header = " > ".join([p for p in [context_prefix, *section] if p])
        emb = f"{header}\n\n{text}" if header else text
        chunk_id = hashlib.sha256(
            f"{version_id}|{STRATEGY}|{CHUNKER_VERSION}|{chash}|{ordinal}".encode()).hexdigest()[:24]
        chunks.append(Chunk(
            ordinal=ordinal, text=text, embedding_text=emb, tokens=count_tokens(text),
            section_path=list(section), curriculum=cur_meta,
            page_start=min(pages) if pages else None, page_end=max(pages) if pages else None,
            page_label=labels[0] if labels else None,
            anchors=sorted({pb.block.anchor for pb in pbs if pb.block.anchor}),
            block_kinds=sorted({pb.block.kind.value for pb in pbs}),
            methods=sorted({pb.method for pb in pbs}),
            min_ocr_confidence=min(confs) if confs else None,
            overlap_prefix_chars=len(overlap) + 1 if overlap else 0,
            chunk_id=chunk_id, section_id=_section_key(section),
        ))
        prev_body = body
    for a, b in zip(chunks, chunks[1:]):
        a.next_chunk_id, b.prev_chunk_id = b.chunk_id, a.chunk_id
    return chunks
