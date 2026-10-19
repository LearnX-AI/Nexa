"""Signed token issue and verify (HS256).

Service and user tokens carry a subject, roles and a type. This is the M1
mechanism. The EduNeX SSO seam in edunex.py replaces issuance for real users
later, without changing how services verify. See NEXA-ADR-0005.
"""
from __future__ import annotations
from typing import Optional
import time
import jwt  # PyJWT

from . import config


class TokenError(Exception):
    pass


def issue_token(subject: str, roles, token_type: str = "service",
                ttl: Optional[int] = None) -> str:
    if not config.IDENTITY_SECRET:
        raise TokenError("NEXA_IDENTITY_SECRET not configured")
    now = int(time.time())
    claims = {
        "sub": subject,
        "roles": list(roles),
        "typ": token_type,
        "iss": config.ISSUER,
        "aud": config.AUDIENCE,
        "iat": now,
        "exp": now + int(ttl or config.DEFAULT_TTL),
    }
    return jwt.encode(claims, config.IDENTITY_SECRET, algorithm="HS256")


def verify_token(token: str) -> dict:
    if not config.IDENTITY_SECRET:
        raise TokenError("NEXA_IDENTITY_SECRET not configured")
    try:
        return jwt.decode(
            token,
            config.IDENTITY_SECRET,
            algorithms=["HS256"],
            audience=config.AUDIENCE,
            issuer=config.ISSUER,
        )
    except Exception as exc:  # signature, expiry, audience, issuer
        raise TokenError(str(exc)) from exc
