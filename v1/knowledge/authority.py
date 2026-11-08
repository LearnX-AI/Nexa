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
