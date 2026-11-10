"""The authenticated principal shared across v1 services. Every service checks
a Principal before model, tool or data access. See NEXA-ADR-0005.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Principal:
    subject: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    token_type: str = "service"  # service or user
    # The school a user belongs to, from the token's school_id claim. Needed to
    # see school-scoped content (NEXA-M2-005, M2-007).
    school_id: Optional[str] = None
    # Other optional claims (lms_site, lms_national, class_ids).
    attrs: dict[str, Any] = field(default_factory=dict)
