"""Edge token verification for the API Gateway.

Verifies the signature and claims of a NEXA identity token (NEXA-ADR-0005) so an
invalid or missing token is rejected before anything is proxied. Permission
enforcement stays downstream in the Agent Runtime, which applies per agent RBAC.
Every rejected request is audited with its reason; the token itself is never
logged.
"""
from __future__ import annotations
from typing import Optional
from fastapi import Header, HTTPException, Request

from v1.identity.principal import Principal
from v1.identity import config as id_config
from v1.identity.tokens import verify_token, TokenError
from v1.observability import audit_event


def _deny(request: Request, status: int, detail: str) -> HTTPException:
    audit_event(
        action="api.authenticate", decision="deny", principal="anonymous",
        reason=detail, path=request.url.path,
        trace_id=getattr(request.state, "trace_id", None),
    )
    return HTTPException(status_code=status, detail=detail)


async def require_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise _deny(request, 401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    if not id_config.IDENTITY_SECRET:
        raise _deny(request, 503, "Identity not configured")
    try:
        claims = verify_token(token)
    except TokenError:
        raise _deny(request, 401, "Invalid token")
    return Principal(
        subject=claims["sub"],
        roles=tuple(claims.get("roles", ())),
        token_type=claims.get("typ", "service"),
    )
