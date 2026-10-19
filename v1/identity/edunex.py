"""EduNeX SSO seam.

Placeholder for M1. When EduNeX or an identity provider issues assertions,
verify them here and return a Principal, so the rest of the platform does not
change. This is the EduNeX to NEXA authentication flow in NEXA-ADR-0005.
"""
from __future__ import annotations

from .principal import Principal


def verify_edunex_assertion(assertion: str) -> Principal:
    raise NotImplementedError(
        "EduNeX SSO verification is a later M1 step. Use service and user "
        "tokens from the identity service for now."
    )
