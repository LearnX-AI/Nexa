"""Identity and policy hook.

Placeholder until centralized identity and RBAC land in NEXA-ADR-0005. For now
the gateway requires a shared bearer token so model endpoints are never open to
end users (NEXA-ADR-0001 security, NEXA-GOV-001 section 12). The returned
Principal is where authenticated identity and role will attach later, so call
sites that depend on it do not change when real identity arrives.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from fastapi import Header, HTTPException

from . import config


@dataclass
class Principal:
    subject: str
    roles: tuple[str, ...] = field(default_factory=tuple)


async def require_principal(
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    # TODO (NEXA-ADR-0005): verify a real identity token and resolve RBAC roles
    # through the identity service. Fail closed until then.
    if not config.GATEWAY_TOKEN:
        raise HTTPException(status_code=503, detail="Gateway auth not configured")
    if authorization != f"Bearer {config.GATEWAY_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    return Principal(subject="service:nexa", roles=("service",))
