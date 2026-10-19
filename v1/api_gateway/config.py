"""API Gateway configuration (NEXA-ARCH-001).

The single external entry for v1. It verifies the caller's identity token at the
edge, applies the CORS policy, and proxies to the Agent Runtime. It holds no
service token: the caller's token is forwarded and the runtime does per agent
RBAC.
"""
from __future__ import annotations
import os

# Upstream Agent Runtime.
RUNTIME_URL = os.environ.get("NEXA_RUNTIME_URL", "http://127.0.0.1:8102")

# Proxy timeout to the runtime, seconds.
REQUEST_TIMEOUT = float(os.environ.get("NEXA_API_TIMEOUT", "120"))

# Bind address for this service.
BIND_HOST = os.environ.get("NEXA_API_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("NEXA_API_PORT", "8099"))

# Allowed browser origins. Same policy as the v0 M0 CORS hardening: EduNeX and
# its subdomains over https. Override with NEXA_CORS_REGEX if needed.
CORS_REGEX = os.environ.get(
    "NEXA_CORS_REGEX", r"https://([a-z0-9-]+\.)*edunex\.edu\.pg$"
)
