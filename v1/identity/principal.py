"""The authenticated principal shared across v1 services. Every service checks
a Principal before model, tool or data access. See NEXA-ADR-0005.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Principal:
    subject: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    token_type: str = "service"  # service or user
