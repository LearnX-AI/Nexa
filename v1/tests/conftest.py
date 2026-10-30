"""Test configuration.

Sets a dummy shared secret and service tokens before any app import, so the
startup preflight passes. No real secrets, no network calls in these tests.
"""
import os

# 32+ bytes so PyJWT does not warn about a short HMAC key.
os.environ.setdefault("NEXA_IDENTITY_SECRET", "test-secret-not-real-0123456789-abcdef")
os.environ.setdefault("NEXA_AGENT_GATEWAY_TOKEN", "test-token")
os.environ.setdefault("NEXA_KNOWLEDGE_GATEWAY_TOKEN", "test-token")
os.environ.pop("NEXA_MODEL_CONFIG", None)

import pytest  # noqa: E402

from v1.identity import tokens  # noqa: E402
from v1.model_gateway import registry  # noqa: E402
from v1.model_gateway.backends import base  # noqa: E402
from v1.observability import metrics  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with default models, closed circuits and empty metrics."""
    registry.reload(None)
    base.reset_breakers()
    metrics.reset()
    yield
    registry.reload(None)
    base.reset_breakers()


@pytest.fixture
def make_token():
    def _make(roles, sub="user:test", token_type="user"):
        return tokens.issue_token(sub, roles, token_type, 300)
    return _make


@pytest.fixture
def auth():
    def _auth(token):
        return {"Authorization": f"Bearer {token}"}
    return _auth
