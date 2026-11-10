"""NEXA-M2-001 authority hierarchy and conflict resolution.

Rules, in the order they are applied (documented in the M2-001 standard):

R1 Eligibility. Only approved sources inside their effective window are
   eligible. Withdrawn, superseded, draft and pending sources never answer.
R2 Version. Among versions of the same source_id, the latest effective_from
   wins; older versions are treated as superseded for answering.
R3 Claim class. What is being decided matters:
   - curriculum_outcome (what students must learn, outcome codes, content
     standards, assessment criteria): only tier 1 (official syllabus) is
     authoritative. Lower tiers may restate it but never override it.
   - instructional (explanations, worked examples, activities): ordered by
     tier, so a teacher guide outranks student material, which outranks LMS.
   - local_context (timetables, school-specific practice): the most specific
     scope wins (class > school > provincial > national), then tier.
R4 Tier. Lower tier number wins (syllabus 1 ... permitted external 6).
R5 Scope. For curriculum_outcome and instructional claims, national outranks
   local within the same tier. Local material supplements, it does not override.
R6 Recency. Within the same tier and scope, the newer effective_from wins.
R7 External last resort. Tier 6 is used only when no tier 1 to 5 source
   covers the question, and the answer must label it as external.

The resolver is deterministic: ties end on source_id so the order is stable.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from .metadata import AuthorityTier, Scope, SourceMetadata

# Retrieval score multiplier per tier. Consumed by the retrieval and reranking
# step; kept here so the hierarchy has one definition. Versioned with the schema.
AUTHORITY_WEIGHTS: dict[AuthorityTier, float] = {
    AuthorityTier.OFFICIAL_SYLLABUS: 1.00,
    AuthorityTier.OFFICIAL_TEACHER_GUIDE: 0.95,
    AuthorityTier.OFFICIAL_STUDENT_MATERIAL: 0.90,
    AuthorityTier.APPROVED_LMS_CONTENT: 0.85,
    AuthorityTier.TEACHER_CREATED: 0.75,
    AuthorityTier.PERMITTED_EXTERNAL: 0.60,
}

_SCOPE_SPECIFICITY = {Scope.NATIONAL: 0, Scope.PROVINCIAL: 1, Scope.SCHOOL: 2, Scope.CLASS: 3}


class ClaimClass(str, Enum):
    CURRICULUM_OUTCOME = "curriculum_outcome"
    INSTRUCTIONAL = "instructional"
    LOCAL_CONTEXT = "local_context"


@dataclass
class Ranked:
    source: SourceMetadata
    rank: int
    authoritative: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class Resolution:
    claim_class: ClaimClass
    ranked: list[Ranked]
    excluded: list[tuple[SourceMetadata, str]]

    @property
    def winner(self) -> Optional[SourceMetadata]:
        for r in self.ranked:
            if r.authoritative:
                return r.source
        return None

    @property
    def external_only(self) -> bool:
        return bool(self.ranked) and all(
            r.source.authority_tier == AuthorityTier.PERMITTED_EXTERNAL for r in self.ranked
        )


def authority_weight(src: SourceMetadata) -> float:
    return AUTHORITY_WEIGHTS[src.authority_tier]


def _latest_versions(sources: list[SourceMetadata]) -> tuple[list[SourceMetadata], list[tuple[SourceMetadata, str]]]:
    by_id: dict[str, list[SourceMetadata]] = {}
    for s in sources:
        by_id.setdefault(s.source_id, []).append(s)
    keep, dropped = [], []
    for versions in by_id.values():
        versions.sort(key=lambda s: (s.effective_from, s.content_checksum), reverse=True)
        keep.append(versions[0])
        for older in versions[1:]:
            dropped.append((older, f"R2 superseded by newer version {versions[0].version_id}"))
    return keep, dropped


def resolve(
    sources: Iterable[SourceMetadata],
    claim_class: ClaimClass = ClaimClass.INSTRUCTIONAL,
    on: Optional[dt.date] = None,
) -> Resolution:
    """Order candidate sources for one claim and mark which are authoritative."""
    on = on or dt.date.today()
    excluded: list[tuple[SourceMetadata, str]] = []
    eligible: list[SourceMetadata] = []
    for s in sources:
        if s.is_retrievable(on):
            eligible.append(s)
        else:
            excluded.append((s, f"R1 not eligible: {s.approval_status.value}, "
                                f"effective {s.effective_from}..{s.effective_to or 'open'}"))

    eligible, superseded = _latest_versions(eligible)
    excluded.extend(superseded)

    if claim_class == ClaimClass.LOCAL_CONTEXT:
        def key(s: SourceMetadata):
            return (-_SCOPE_SPECIFICITY[s.scope], s.authority_tier.value,
                    -s.effective_from.toordinal(), s.source_id)
    else:
        def key(s: SourceMetadata):
            return (s.authority_tier.value, _SCOPE_SPECIFICITY[s.scope],
                    -s.effective_from.toordinal(), s.source_id)

    ordered = sorted(eligible, key=key)
    has_internal = any(s.authority_tier != AuthorityTier.PERMITTED_EXTERNAL for s in ordered)

    ranked: list[Ranked] = []
    for i, s in enumerate(ordered):
        reasons = [f"tier {s.authority_tier.value} ({s.source_type.value})", f"scope {s.scope.value}"]
        if claim_class == ClaimClass.CURRICULUM_OUTCOME:
            authoritative = s.authority_tier == AuthorityTier.OFFICIAL_SYLLABUS
            if not authoritative:
                reasons.append("R3 outcomes are defined only by the official syllabus")
        elif s.authority_tier == AuthorityTier.PERMITTED_EXTERNAL and has_internal:
            authoritative = False
            reasons.append("R7 external used only when no curriculum source covers the question")
        else:
            authoritative = True
        ranked.append(Ranked(source=s, rank=i + 1, authoritative=authoritative, reasons=reasons))
    return Resolution(claim_class=claim_class, ranked=ranked, excluded=excluded)


# ---------------------------------------------------------------- retrieval (NEXA-M2-005)
#
# resolve() orders whole sources for one claim. At retrieval time NEXA has
# ranked passages, and must stop a lower-authority passage from silently
# standing in for an official one. arbitrate() applies R3, R4, R5 and R7 to a
# ranked result list:
#
# - Two passages "cover the same point" when they share an outcome code or a
#   topic code, or (failing codes) the same subject and module code. This is a
#   structural test: NEXA does not try to decide whether two texts contradict
#   each other, only which one is authoritative where both apply.
# - Within a group, a passage from a higher-authority source (lower tier, or
#   national over local in the same tier) that ranks below a lower-authority
#   one is moved up to just above it. Both stay in the results, so local and
#   LMS material still supplements the official text; it just never replaces
#   it. Each move is recorded on both passages and in the conflict list.
# - For a curriculum-outcome question (R3) every tier-1 passage is placed
#   first and only tier 1 is marked authoritative.
# - Permitted external sources (tier 6) go after every curriculum source (R7)
#   and are marked external.
# Rank order is otherwise kept, so relevance still decides.

import re as _re

_OUTCOME_Q = _re.compile(
    r"\b(learning outcomes?|outcomes?|content standards?|assessment criteria|syllabus|what (must|should|do) "
    r"(students|learners|i) (learn|know)|\d{1,2}\.\d{1,2}\.\d{1,2}[a-z]?)\b", _re.I)


def classify_claim(query: str) -> ClaimClass:
    """A query about what must be learned is a curriculum_outcome claim;
    everything else retrieval serves is instructional."""
    return ClaimClass.CURRICULUM_OUTCOME if _OUTCOME_Q.search(query or "") else ClaimClass.INSTRUCTIONAL


def _tier(md: dict) -> int:
    try:
        return int(md.get("authority_tier"))
    except (TypeError, ValueError):
        return 7  # no source contract (M1 raw documents): below every tier


def _keys(md: dict) -> set[str]:
    keys = {f"outcome:{c}" for c in str(md.get("outcome_codes") or "").split(",") if c}
    if md.get("topic_code"):
        keys.add(f"topic:{md['topic_code']}")
    if not keys and md.get("module_code") and md.get("subject"):
        keys.add(f"module:{md['subject']}:{md['module_code']}")
    return keys


def _rank_key(md: dict) -> tuple[int, int]:
    """Lower is more authoritative: tier first, then scope (R5)."""
    scope = {"national": 0, "provincial": 1, "school": 2, "class": 3}.get(md.get("scope") or "", 4)
    return _tier(md), scope


def arbitrate(results: list[dict], claim_class: ClaimClass = ClaimClass.INSTRUCTIONAL
              ) -> tuple[list[dict], list[dict]]:
    """Reorder ranked passages by the authority rules. Each passage gains an
    'authority' block; returns (results, conflicts)."""
    out = [dict(r) for r in results]
    conflicts: list[dict] = []
    for r in out:
        md = r.get("metadata") or {}
        r["authority"] = {"tier": _tier(md) if _tier(md) < 7 else None, "authoritative": True,
                          "external": _tier(md) == 6, "notes": []}

    # R3: outcome questions are answered by the syllabus.
    if claim_class == ClaimClass.CURRICULUM_OUTCOME and any(_tier(r["metadata"]) == 1 for r in out):
        tier1 = [r for r in out if _tier(r["metadata"]) == 1]
        rest = [r for r in out if _tier(r["metadata"]) != 1]
        for r in rest:
            r["authority"]["authoritative"] = False
            r["authority"]["notes"].append("R3: learning outcomes are defined only by the official syllabus")
        if rest and out.index(tier1[0]) > 0:
            conflicts.append({"rule": "R3", "promoted": [r["chunk_id"] for r in tier1],
                              "over": [r["chunk_id"] for r in rest if out.index(r) < out.index(tier1[-1])]})
        out = tier1 + rest

    # R4/R5: within a point both cover, the more authoritative passage leads.
    # Each move removes at least one authority inversion, so this ends.
    changed = True
    guard = 0
    while changed and guard <= len(out) ** 2:
        changed, guard = False, guard + 1
        for i, low in enumerate(out):
            kl = _keys(low["metadata"])
            if not kl:
                continue
            for j in range(i + 1, len(out)):
                high = out[j]
                shared = kl & _keys(high["metadata"])
                if not shared or high["metadata"].get("source_id") == low["metadata"].get("source_id"):
                    continue
                if _rank_key(high["metadata"]) < _rank_key(low["metadata"]):
                    out.insert(i, out.pop(j))
                    point = sorted(shared)[0]
                    high["authority"]["notes"].append(
                        f"R4: placed above lower-authority {low['metadata'].get('title')!r} on {point}")
                    low["authority"]["notes"].append(
                        f"R4: {high['metadata'].get('title')!r} is more authoritative on {point}; "
                        "this passage supplements it")
                    low["authority"]["authoritative"] = False
                    conflicts.append({"rule": "R4", "point": point, "authoritative": high["chunk_id"],
                                      "authoritative_tier": _tier(high["metadata"]),
                                      "lower": low["chunk_id"], "lower_tier": _tier(low["metadata"])})
                    changed = True
                    break
            if changed:
                break

    # R7: external sources only after every curriculum source.
    if any(_tier(r["metadata"]) < 6 for r in out):
        internal = [r for r in out if _tier(r["metadata"]) != 6]
        external = [r for r in out if _tier(r["metadata"]) == 6]
        for r in external:
            r["authority"]["authoritative"] = False
            r["authority"]["notes"].append("R7: external source, used only after curriculum sources")
        out = internal + external
    return out, conflicts
