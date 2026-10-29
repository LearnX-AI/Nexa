"""NEXA Model Gateway configuration.

All values come from the environment. No secrets in source (NEXA-GOV-001,
NEXA-ADR-0005). See NEXA-ADR-0001 for the gateway decision.
"""
from __future__ import annotations
import os

# Upstream runtimes the gateway fronts in M1.
OLLAMA_BASE_URL = os.getenv("NEXA_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OPENAI_BASE_URL = os.getenv("NEXA_OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Gateway auth. Shared bearer token so model endpoints are never open to end
# users. This is a placeholder until centralized identity and RBAC land in
# ADR-0005.
GATEWAY_TOKEN = os.getenv("NEXA_GATEWAY_TOKEN", "")

# Upstream request timeout in seconds.
REQUEST_TIMEOUT = float(os.getenv("NEXA_GATEWAY_TIMEOUT", "60"))
