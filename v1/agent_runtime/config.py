"""Agent Runtime configuration (NEXA-ADR-0003).

The runtime presents a service token to the Model Gateway and verifies the end
user's principal with the shared identity secret. The incoming principal is
checked against the agent's required permission; the downstream call to the
gateway uses the runtime's own service token.
"""
from __future__ import annotations
import os

# Where the Model Gateway listens.
GATEWAY_URL = os.environ.get("NEXA_GATEWAY_URL", "http://127.0.0.1:8100")

# Service token the runtime presents to the gateway. Minted from the identity
# CLI with role service. Required in any real deployment.
GATEWAY_TOKEN = os.environ.get("NEXA_AGENT_GATEWAY_TOKEN", "")

# Request timeout to the gateway, seconds.
GATEWAY_TIMEOUT = float(os.environ.get("NEXA_GATEWAY_TIMEOUT", "120"))

# Bind address for this service.
BIND_HOST = os.environ.get("NEXA_AGENT_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("NEXA_AGENT_PORT", "8102"))
