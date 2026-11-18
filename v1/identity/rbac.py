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
    "ai_engineer": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest", "knowledge.admin"},
    "teacher": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    "student": {"model.invoke", "knowledge.query"},
    "school_admin": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    "national_admin": {"model.list", "knowledge.query"},
    "auditor": {"model.list", "knowledge.query"},
    "admin": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest", "admin", "knowledge.admin"},
    # Baseline roles named in NEXA-M1-005. platform_admin is the full
    # administrator; service_account is the non-human service identity.
    "platform_admin": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest", "admin", "knowledge.admin"},
    "service_account": {"model.invoke", "model.list", "knowledge.query", "knowledge.ingest"},
    # An LMS site's sync client (NEXA-M2-007). It can push and reconcile its own
    # site's content and nothing else: no queries, no direct uploads.
    "lms_sync": {"knowledge.lms.sync"},
    # A school NEXA server (NEXA-M2-009): fetches its own school's packages and
    # forwards answer requests on behalf of its students and teachers.
    "edge_node": {"knowledge.query", "knowledge.edge.sync"},
}
for _r in ("admin", "platform_admin"):
    ROLE_PERMISSIONS[_r] |= {"knowledge.lms.sync", "knowledge.lms.admin"}
ROLE_PERMISSIONS["ai_engineer"] |= {"knowledge.lms.admin"}

# Agent registry (NEXA-M3-002). Reading it is open to every human and service
# role; changing it is not. agents.register: submit a manifest (a new agent or
# version). agents.approve: move a version through review, approval and
# activation. agents.admin: suspend, deprecate, disable (the kill switch).
# agents.deploy: an Agent Runtime instance recording where it runs and its health.
for _r in ROLE_PERMISSIONS:
    if _r not in ("lms_sync",):
        ROLE_PERMISSIONS[_r] |= {"agents.read"}
for _r in ("ai_engineer", "admin", "platform_admin"):
    ROLE_PERMISSIONS[_r] |= {"agents.register", "agents.admin"}
for _r in ("admin", "platform_admin", "national_admin"):
    ROLE_PERMISSIONS[_r] |= {"agents.approve"}
for _r in ("service", "service_account", "admin", "platform_admin"):
    ROLE_PERMISSIONS[_r] |= {"agents.deploy"}
# Tool Gateway (NEXA-M3-004): only the Agent Runtime's service identity calls
# tools, on behalf of a user and an agent.
for _r in ("service", "service_account"):
    ROLE_PERMISSIONS[_r] |= {"tools.invoke"}


def permissions_for(roles) -> set:
    perms: set = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, set())
    return perms


def can(principal: Principal, permission: str) -> bool:
    return permission in permissions_for(principal.roles)
