"""Role based access control.

Framework agnostic: can() returns a bool and callers raise their own error.
Roles follow the ADR-0005 permission matrix at a coarse M1 level and are
refined as more services arrive. See NEXA-ADR-0005.
"""
from __future__ import annotations

from .principal import Principal

# role -> set of permissions the role holds
ROLE_PERMISSIONS = {
    "service": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    "ai_engineer": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    "teacher": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    "student": {"model.invoke", "knowledge.query"},
    "school_admin": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    "national_admin": {"model.list", "knowledge.query"},
    "auditor": {"model.list", "knowledge.query"},
    "admin": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest", "admin"},
}


def permissions_for(roles) -> set:
    perms: set = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, set())
    return perms


def can(principal: Principal, permission: str) -> bool:
    return permission in permissions_for(principal.roles)
