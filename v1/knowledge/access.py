"""Who may retrieve what (NEXA-M2-001 access_classification, NEXA-GOV-001).

Least privilege. A service token gets learner-level content only, because an
agent may be acting for a student; passing the end user's roles through to the
Knowledge Service is an M3 agent concern. School and class scoped sources need
a school claim on the principal, which identity does not issue yet, so they are
excluded from retrieval for everyone except administrators until it does.

The filter is a value object so each store applies it natively: the in-memory
store calls it on metadata, the pgvector store renders it as SQL.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

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


def allowed_classes(principal: Principal) -> set[str]:
    out: set[str] = set()
    for r in principal.roles:
        out |= ROLE_ACCESS.get(r, set())
    return out


@dataclass(frozen=True)
class RetrievalFilter:
    classes: frozenset[str]
    today: str
    local_scope: bool

    def __call__(self, md: dict[str, Any]) -> bool:
        if md.get("approval_status") is None:
            return True  # documents ingested through the M1 raw endpoint carry no source contract
        if md.get("approval_status") != "approved":
            return False
        if md.get("access_classification") not in self.classes:
            return False
        if md.get("effective_from") and md["effective_from"] > self.today:
            return False
        if md.get("effective_to") and md["effective_to"] < self.today:
            return False
        if md.get("scope") in ("school", "class") and not self.local_scope:
            return False
        return True

    def sql(self) -> tuple[str, list[Any]]:
        """WHERE clause over the filter columns of a pgvector index table."""
        clause = (
            "(approval_status IS NULL OR ("
            "approval_status = 'approved' AND access_classification = ANY(%s) "
            "AND (effective_from IS NULL OR effective_from <= %s::date) "
            "AND (effective_to IS NULL OR effective_to >= %s::date)"
            + ("" if self.local_scope else " AND scope NOT IN ('school', 'class')")
            + "))"
        )
        return clause, [sorted(self.classes), self.today, self.today]


def retrieval_filter(principal: Principal, on: dt.date | None = None) -> RetrievalFilter:
    """Approved, in effect, permitted class, and national or provincial scope
    unless the caller is an administrator."""
    return RetrievalFilter(
        classes=frozenset(allowed_classes(principal)),
        today=(on or dt.date.today()).isoformat(),
        local_scope=bool(set(principal.roles) & _SCOPE_ADMINS),
    )
