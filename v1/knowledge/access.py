"""Who may retrieve what (NEXA-M2-001 access_classification, NEXA-GOV-001).

Least privilege. A service token gets learner-level content only, because an
agent may be acting for a student; passing the end user's roles through to the
Knowledge Service is an M3 agent concern. School and class scoped sources need
a school claim on the principal, which identity does not issue yet, so they are
excluded from retrieval for everyone except administrators until it does.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from v1.identity.principal import Principal

_LEARNER = {"public", "learner"}
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


def retrieval_filter(principal: Principal, on: dt.date | None = None) -> Callable[[dict[str, Any]], bool]:
    """Predicate over chunk metadata: approved, in effect, permitted class, and
    national or provincial scope unless the caller is an administrator."""
    classes = allowed_classes(principal)
    today = (on or dt.date.today()).isoformat()
    scope_ok_all = bool(set(principal.roles) & _SCOPE_ADMINS)

    def ok(md: dict[str, Any]) -> bool:
        if "approval_status" not in md:
            return True  # documents ingested through the M1 raw endpoint carry no source contract
        if md.get("approval_status") != "approved":
            return False
        if md.get("access_classification") not in classes:
            return False
        if md.get("effective_from") and md["effective_from"] > today:
            return False
        if md.get("effective_to") and md["effective_to"] < today:
            return False
        if md.get("scope") in ("school", "class") and not scope_ok_all:
            return False
        return True

    return ok
