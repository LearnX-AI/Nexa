"""Endpoint tests for health and auth boundaries (NEXA-M1-002, M1-010).

No network: health needs none, and protected routes reject before any upstream
call. The in-process TestClient triggers the startup preflight, which passes
because conftest set the dummy secrets.
"""
from fastapi.testclient import TestClient

from v1.api_gateway.main import app as api_app
from v1.knowledge.main import app as know_app
from v1.agent_runtime.main import app as agent_app


def test_api_gateway_health():
    with TestClient(api_app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "nexa-api-gateway"


def test_api_gateway_requires_token():
    with TestClient(api_app) as c:
        r = c.get("/v1/agents")
        assert r.status_code == 401
        # standardized error shape carries a trace id
        assert "error" in r.json()


def test_knowledge_health_and_auth():
    with TestClient(know_app) as c:
        assert c.get("/health").status_code == 200
        r = c.post("/v1/knowledge/query", json={"query": "x"})
        assert r.status_code == 401


def test_agent_runtime_health_and_auth():
    with TestClient(agent_app) as c:
        assert c.get("/health").status_code == 200
        r = c.post("/v1/agents/nexa-tutor/run", json={"messages": []})
        assert r.status_code == 401
