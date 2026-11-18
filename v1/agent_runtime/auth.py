"""Incoming principal verification for the Agent Runtime.

Verifies a signed NEXA identity token (NEXA-ADR-0005) and returns the Principal.
It does not enforce a permission here; the runtime enforces the agent's required
permission once the agent is known.
"""
from __future__ import annotations
from typing import Optional
from fastapi import Header, HTTPException

from v1.identity.principal import Principal
from v1.identity import config as id_config
from v1.identity.tokens import verify_token, TokenError


async def require_principal(
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    if not id_config.IDENTITY_SECRET:
        raise HTTPException(status_code=503, detail="Identity not configured")
    try:
        claims = verify_token(token)
    except TokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if claims.get("typ") == "delegated":
        # Delegated tokens (NEXA-M3-007) are for the Tool Gateway only: one an
        # agent's tool call carried can never be replayed as the user here.
        raise HTTPException(status_code=401, detail="A delegated token cannot be used here")
    return principal_from_claims(claims)


def principal_from_claims(claims: dict) -> Principal:
    """The tenant context travels with the principal (NEXA-M3-007): the user's
    school and classes, for decisions and for the tokens the runtime mints."""
    return Principal(
        subject=claims["sub"],
        roles=tuple(claims.get("roles", ())),
        token_type=claims.get("typ", "service"),
        school_id=claims.get("school_id"),
        attrs={k: claims[k] for k in ("class_ids", "lms_site", "lms_national", "act", "scope", "nexa_aud", "run_id")
               if k in claims},
    )
