"""Who may retrieve what (NEXA-M2-001 access_classification, NEXA-GOV-001), and
what a request narrows retrieval to (NEXA-M2-005).

Least privilege. A service token gets learner-level content only, because an
agent may be acting for a student; passing the end user's roles through to the
Knowledge Service is an M3 agent concern.

Scope. National and provincial content is visible to everyone allowed the
access class. School-scoped content is visible only to a principal whose token
carries the same school_id claim (NEXA-M2-007), and to administrators.
Class-scoped content needs class membership, which identity does not issue
yet, so only administrators see it.

Request filters (subject, grade, language, curriculum codes, source type,
authority tier, scope) only ever narrow what access allows; they can never
widen it. A field a filter names must be present on the passage: a passage
with no subject never matches a subject filter.

The filter is a value object so each store applies it natively: the in-memory
store calls it on metadata, the pgvector store renders it as SQL over typed
columns. The two are tested for parity.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from v1.identity.principal import Principal

_LEARNER = frozenset({"public", "learner"})
_STAFF = _LEARNER | {"teacher_only"}
_ALL = _STAFF | {"restricted"}

ROLE_ACCESS = {
    "student": _LEARNER,
    "service": _LEARNER,
    "service_account": _LEARNER,
    "teacher": _STAFF,
    "school_admin": _STAFF,
    "ai_engineer": _STAFF,
    "auditor": _STAFF,
    "national_admin": _ALL,
    "admin": _ALL,
    "platform_admin": _ALL,
}
_SCOPE_ADMINS = {"admin", "platform_admin", "national_admin"}

# Request filter name -> metadata key. Set-valued filters match when the
# passage's value is one of the requested values.
NARROWING = ("subjects", "grades", "languages", "source_types", "module_codes", "topic_codes", "scopes")


def allowed_classes(principal: Principal) -> set[str]:
    out: set[str] = set()
    for r in principal.roles:
        out |= ROLE_ACCESS.get(r, set())
    return out


def _grades(v: Any) -> set[int]:
    if v is None or v == "":
        return set()
    items = v if isinstance(v, (list, tuple)) else str(v).split(",")
    out = set()
    for x in items:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _fs(v: Optional[Iterable[Any]], cast=str) -> Optional[frozenset]:
    if v is None:
        return None
    vals = frozenset(cast(x) for x in v)
    return vals or None


@dataclass(frozen=True)
class RetrievalFilter:
    classes: frozenset[str]
    today: str
    local_scope: bool                      # administrators: every school and class
    school_id: Optional[str] = None        # the principal's school (token claim)
    subjects: Optional[frozenset[str]] = None
    grades: Optional[frozenset[int]] = None
    languages: Optional[frozenset[str]] = None
    source_types: Optional[frozenset[str]] = None
    module_codes: Optional[frozenset[str]] = None
    topic_codes: Optional[frozenset[str]] = None
    scopes: Optional[frozenset[str]] = None
    max_tier: Optional[int] = None         # e.g. 3 = official material only

    def narrow(self, **req: Any) -> "RetrievalFilter":
        """A copy restricted further by request filters. Unknown keys are refused."""
        unknown = set(req) - set(NARROWING) - {"max_tier"}
        if unknown:
            raise ValueError(f"unknown filters: {sorted(unknown)}")
        upd: dict[str, Any] = {}
        for k in NARROWING:
            if req.get(k) is not None:
                upd[k] = _fs(req[k], int if k == "grades" else str)
        if req.get("max_tier") is not None:
            upd["max_tier"] = int(req["max_tier"])
        return dataclasses.replace(self, **upd)

    def describe(self) -> dict[str, Any]:
        """What was applied, for the explain payload (no secrets in here)."""
        d = {"access_classes": sorted(self.classes), "on": self.today,
             "scope": "all" if self.local_scope else ("national, provincial and school " + self.school_id
                                                       if self.school_id else "national and provincial")}
        for k in NARROWING:
            v = getattr(self, k)
            if v is not None:
                d[k] = sorted(v)
        if self.max_tier is not None:
            d["max_tier"] = self.max_tier
        return d

    def __call__(self, md: dict[str, Any]) -> bool:
        if md.get("approval_status") is not None:
            if md.get("approval_status") != "approved":
                return False
            if md.get("access_classification") not in self.classes:
                return False
            if md.get("effective_from") and md["effective_from"] > self.today:
                return False
            if md.get("effective_to") and md["effective_to"] < self.today:
                return False
            if md.get("scope") in ("school", "class") and not self.local_scope:
                if not (md.get("scope") == "school" and self.school_id and md.get("school_id") == self.school_id):
                    return False
        # documents ingested through the M1 raw endpoint carry no source
        # contract: they pass access, but never match a narrowing filter.
        if self.subjects is not None and md.get("subject") not in self.subjects:
            return False
        if self.grades is not None and not (_grades(md.get("grade_levels")) & self.grades):
            return False
        if self.languages is not None and md.get("language") not in self.languages:
            return False
        if self.source_types is not None and md.get("source_type") not in self.source_types:
            return False
        if self.module_codes is not None and md.get("module_code") not in self.module_codes:
            return False
        if self.topic_codes is not None and md.get("topic_code") not in self.topic_codes:
            return False
        if self.scopes is not None and md.get("scope") not in self.scopes:
            return False
        if self.max_tier is not None:
            tier = md.get("authority_tier")
            if tier is None or int(tier) > self.max_tier:
                return False
        return True

    def sql(self) -> tuple[str, list[Any]]:
        """WHERE clause over the typed filter columns of a pgvector index table."""
        parts = [
            "(approval_status IS NULL OR ("
            "approval_status = 'approved' AND access_classification = ANY(%s) "
            "AND (effective_from IS NULL OR effective_from <= %s::date) "
            "AND (effective_to IS NULL OR effective_to >= %s::date)"
            + ("" if self.local_scope else
               " AND (scope IS NULL OR scope NOT IN ('school', 'class') OR (scope = 'school' AND school_id = %s))")
            + "))"
        ]
        params: list[Any] = [sorted(self.classes), self.today, self.today]
        if not self.local_scope:
            params.append(self.school_id)
        cols = {"subjects": "subject", "languages": "language", "source_types": "source_type",
                "module_codes": "module_code", "topic_codes": "topic_code", "scopes": "scope"}
        for k, col in cols.items():
            v = getattr(self, k)
            if v is not None:
                parts.append(f"{col} = ANY(%s)")
                params.append(sorted(v))
        if self.grades is not None:
            parts.append("grade_levels && %s::int[]")
            params.append(sorted(self.grades))
        if self.max_tier is not None:
            parts.append("authority_tier <= %s")
            params.append(self.max_tier)
        return " AND ".join(parts), params


def retrieval_filter(principal: Principal, on: dt.date | None = None) -> RetrievalFilter:
    """Approved, in effect, permitted class; national and provincial scope, plus
    the principal's own school, or everything for an administrator."""
    return RetrievalFilter(
        classes=frozenset(allowed_classes(principal)),
        today=(on or dt.date.today()).isoformat(),
        local_scope=bool(set(principal.roles) & _SCOPE_ADMINS),
        school_id=principal.school_id,
    )
