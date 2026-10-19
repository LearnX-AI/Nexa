"""Role based access control.

Framework agnostic: can() returns a bool and callers raise their own error.
Roles follow the ADR-0005 permission matrix at a coarse M1 level and are
refined as more services arrive. See NEXA-ADR-0005.
"""
from __future__ import annotations

from .principal import Principal

# permission -> set of roles that hold it
ROLE_PERMISSIONS = {
    "service": {"model.invoke", "model.list"},
    "ai_engineer": {"model.invoke", "model.list"},
    "teacher": {"model.invoke", "model.list"},
    "student": {"model.invoke"},
    "school_admin": {"model.invoke", "model.list"},
    "national_admin": {"model.list"},
    "auditor": {"model.list"},
    "admin": {"model.invoke", "model.list", "admin"},
}


def permissions_for(roles) -> set:
    perms: set = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, set())
    return perms


def can(principal: Principal, permission: str) -> bool:
    return permission in permissions_for(principal.roles)
