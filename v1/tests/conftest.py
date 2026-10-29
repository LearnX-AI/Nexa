"""Test configuration.

Sets a dummy shared secret and service tokens before any app import, so the
startup preflight passes. No real secrets, no network calls in these tests.
"""
import os

os.environ.setdefault("NEXA_IDENTITY_SECRET", "test-secret-not-real")
os.environ.setdefault("NEXA_AGENT_GATEWAY_TOKEN", "test-token")
os.environ.setdefault("NEXA_KNOWLEDGE_GATEWAY_TOKEN", "test-token")
