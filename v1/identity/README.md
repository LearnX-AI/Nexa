# NEXA Identity and Policy (M1 skeleton)

Authenticated identity and RBAC for v1 (NEXA-ADR-0005). Every v1 service checks
a `Principal` before model, tool or data access. M1 uses signed service and
user tokens; the EduNeX SSO flow plugs into `edunex.py` later without changing
how services verify.

## Parts

- `tokens.py`  Issue and verify HS256 signed tokens (subject, roles, type).
- `rbac.py`  Roles to permissions, `can(principal, permission)`.
- `principal.py`  The shared `Principal`.
- `edunex.py`  SSO seam, not implemented yet.
- `service.py`  Small FastAPI service: `/health`, `/introspect`, and `/token`
  (dev issuance only, guarded by `NEXA_IDENTITY_ALLOW_ISSUE=1`).
- `cli.py`  Mint a token from the command line to bootstrap service callers.

## Config (environment)

- `NEXA_IDENTITY_SECRET`  signing secret, required.
- `NEXA_IDENTITY_ISSUER`  default `nexa-identity`.
- `NEXA_IDENTITY_AUDIENCE`  default `nexa`.
- `NEXA_IDENTITY_TTL`  default token lifetime in seconds.
- `NEXA_IDENTITY_ALLOW_ISSUE=1`  allow HTTP token issuance, dev only.

## Gateway integration

The Model Gateway verifies these tokens and enforces the `model.invoke`
permission. If `NEXA_IDENTITY_SECRET` is not set it falls back to the legacy
shared gateway token, so migration is safe. Set the same secret on both
services and mint a service token to switch the gateway to identity tokens.

## Not in this skeleton, by design

EduNeX or IdP token verification, refresh tokens, key rotation and a user
directory. Those are later M1 steps. Do not add future-phase work here
(NEXA-DEV-001 section 15).
