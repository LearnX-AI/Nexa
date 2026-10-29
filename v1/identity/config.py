"""NEXA Identity configuration. Values from the environment, no secrets in
source (NEXA-GOV-001). See NEXA-ADR-0005.
"""
from __future__ import annotations
import os

# Signing secret for service and user tokens. Required to issue or verify.
IDENTITY_SECRET = os.getenv("NEXA_IDENTITY_SECRET", "")

ISSUER = os.getenv("NEXA_IDENTITY_ISSUER", "nexa-identity")
AUDIENCE = os.getenv("NEXA_IDENTITY_AUDIENCE", "nexa")
DEFAULT_TTL = int(os.getenv("NEXA_IDENTITY_TTL", "3600"))

# Dev only. Token issuance over HTTP is allowed only when this is 1. In
# production, user tokens come from the EduNeX SSO seam (edunex.py).
ALLOW_ISSUE = os.getenv("NEXA_IDENTITY_ALLOW_ISSUE", "") == "1"
