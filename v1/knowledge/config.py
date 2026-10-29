"""Knowledge Service configuration (NEXA-ADR-0004).

Embeddings are obtained through the Model Gateway, never by importing an
embedding SDK here. The service presents its own service token to the gateway.
The vector store is pluggable; the M1 skeleton ships an in-memory backend and
defers the pgvector backend (NEXA-ADR-0002) to its own step.
"""
from __future__ import annotations
import os

# Model Gateway for embeddings.
GATEWAY_URL = os.environ.get("NEXA_GATEWAY_URL", "http://127.0.0.1:8100")

# Service token the Knowledge Service presents to the gateway. Minted from the
# identity CLI with role service. Required in any real deployment.
GATEWAY_TOKEN = os.environ.get("NEXA_KNOWLEDGE_GATEWAY_TOKEN", "")

# Logical embedding model name resolved by the gateway registry.
EMBED_MODEL = os.environ.get("NEXA_EMBED_MODEL", "nexa-embed-default")

# Request timeout to the gateway, seconds.
GATEWAY_TIMEOUT = float(os.environ.get("NEXA_GATEWAY_TIMEOUT", "120"))

# Which store backend to use: memory (default for the skeleton) or pgvector
# (deferred, raises if selected until implemented).
STORE_BACKEND = os.environ.get("NEXA_KNOWLEDGE_STORE", "memory")

# Default number of results per query.
DEFAULT_TOP_K = int(os.environ.get("NEXA_KNOWLEDGE_TOP_K", "5"))

# Bind address for this service.
BIND_HOST = os.environ.get("NEXA_KNOWLEDGE_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("NEXA_KNOWLEDGE_PORT", "8103"))
