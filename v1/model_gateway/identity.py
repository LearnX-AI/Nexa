"""Identity and policy hook for the Model Gateway.

Verifies a NEXA identity token (NEXA-ADR-0005) and enforces RBAC before any
model call. When NEXA_IDENTITY_SECRET is not set it falls back to the legacy
shared gateway token, so an existing deployment keeps working during the switch
to identity tokens. Principal is re-exported from the shared identity package
so callers stay stable.
"""
from __future__ import annotations
from typing import Optional
from fastapi import Header, HTTPException

from . import config
from v1.identity.principal import Principal
from v1.identity import config as id_config
from v1.identity.tokens import verify_token, TokenError
from v1.identity.rbac import can

__all__ = ["Principal", "require_principal"]


async def require_principal(
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]

    # Preferred path: verify a signed NEXA identity token and enforce RBAC.
    if id_config.IDENTITY_SECRET:
        try:
            claims = verify_token(token)
        except TokenError:
            raise HTTPException(status_code=401, detail="Invalid token")
        principal = Principal(
            subject=claims["sub"],
            roles=tuple(claims.get("roles", ())),
            token_type=claims.get("typ", "service"),
        )
        if not can(principal, "model.invoke"):
            raise HTTPException(status_code=403, detail="Not permitted")
        return principal

    # Fallback: legacy shared token. Removed once all callers use identity
    # tokens (NEXA-ADR-0005).
    if not config.GATEWAY_TOKEN:
        raise HTTPException(status_code=503, detail="Gateway auth not configured")
    if token != config.GATEWAY_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return Principal(subject="service:nexa", roles=("service",))
