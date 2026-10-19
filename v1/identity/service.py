"""NEXA Identity service (M1 skeleton).

Verifies tokens and, in dev only, issues them. Real user tokens come from the
EduNeX SSO seam (edunex.py) later. See NEXA-ADR-0005.

Run:
    export NEXA_IDENTITY_SECRET=some-strong-secret
    export NEXA_IDENTITY_ALLOW_ISSUE=1          # dev only
    uvicorn v1.identity.service:app --host 127.0.0.1 --port 8110
"""
from __future__ import annotations
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import config, tokens
from .rbac import permissions_for

app = FastAPI(title="NEXA Identity", version="0.1.0")


class IssueRequest(BaseModel):
    subject: str
    roles: list[str] = []
    type: str = "service"
    ttl: Optional[int] = None


class IntrospectRequest(BaseModel):
    token: str


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexa-identity", "version": "0.1.0"}


@app.post("/token")
async def issue(req: IssueRequest):
    if not config.ALLOW_ISSUE:
        raise HTTPException(status_code=403, detail="Token issuance disabled")
    try:
        token = tokens.issue_token(req.subject, req.roles, req.type, req.ttl)
    except tokens.TokenError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"token": token, "token_type": "Bearer"}


@app.post("/introspect")
async def introspect(req: IntrospectRequest):
    try:
        claims = tokens.verify_token(req.token)
    except tokens.TokenError:
        return {"active": False}
    return {
        "active": True,
        "sub": claims["sub"],
        "roles": claims.get("roles", []),
        "typ": claims.get("typ"),
        "exp": claims.get("exp"),
        "permissions": sorted(permissions_for(claims.get("roles", []))),
    }
