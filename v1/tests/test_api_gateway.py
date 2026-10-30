"""API Gateway tests (NEXA-M1-002): versioned routes, schemas, standardized
errors, correlation ids, auth hook, audit of denials, OpenAPI. The runtime is a
mock transport, so the real proxy code runs without a network."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.api_gateway import main as api

RUN_OK = {
    "schema_version": "1", "agent": "nexa-tutor", "agent_version": "0.1.0",
    "model": "nexa-chat-default", "output": "DNA is ...", "trace_id": "t",
    "usage": {"completion_tokens": 3}, "tool_calls": [], "x_nexa": {"runtime": "ollama"},
}
AGENTS = {"agents": [{
    "name": "nexa-tutor", "description": "d", "model": "m", "version": "0.1.0",
    "deprecated": False, "capabilities": ["chat"], "tools": []}]}


@pytest.fixture
def seen():
    return []


@pytest.fixture
def client(monkeypatch, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json=AGENTS)
        if request.url.path.endswith("/run"):
            name = request.url.path.split("/")[3]
            if name == "forbidden-agent":
                return httpx.Response(403, json={"detail": "Not permitted"})
            return httpx.Response(200, json=RUN_OK)
        return httpx.Response(404, json={"detail": "nope"})

    monkeypatch.setattr(api, "_transport", httpx.MockTransport(handler))
    with TestClient(api.app) as c:
        yield c


def _run(client, headers, name="nexa-tutor", body=None):
    body = body or {"messages": [{"role": "user", "content": "What is DNA?"}]}
    return client.post(f"/v1/agents/{name}/run", headers=headers, json=body)


def test_health_and_ready(client):
    assert client.get("/health").json()["service"] == "nexa-api-gateway"
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["runtime_ok"] is True


def test_ready_degraded_when_runtime_down(monkeypatch):
    def down(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(api, "_transport", httpx.MockTransport(down))
    with TestClient(api.app) as c:
        r = c.get("/ready")
    assert r.status_code == 503 and r.json()["status"] == "degraded"


def test_list_and_run_forward_token_and_trace(client, seen, make_token, auth):
    token = make_token(["teacher"])
    h = {**auth(token), "X-NEXA-Trace-Id": "trace-xyz"}
    assert client.get("/v1/agents", headers=h).json() == AGENTS
    r = _run(client, h)
    assert r.status_code == 200 and r.json()["output"] == "DNA is ..."
    assert r.headers["x-nexa-trace-id"] == "trace-xyz"
    upstream = seen[-1]
    assert upstream.headers["authorization"] == f"Bearer {token}"
    assert upstream.headers["x-nexa-trace-id"] == "trace-xyz"
    assert json.loads(upstream.content)["messages"][0]["content"] == "What is DNA?"


def test_missing_and_invalid_token_rejected_with_standard_error(client, auth, capsys):
    for headers in ({}, auth("not-a-token")):
        r = client.get("/v1/agents", headers=headers)
        assert r.status_code == 401
        err = r.json()["error"]
        assert err["status"] == 401 and err["trace_id"]
        assert r.headers["x-nexa-trace-id"] == err["trace_id"]
    audits = [json.loads(l) for l in capsys.readouterr().out.splitlines() if '"event": "audit"' in l]
    assert len(audits) == 2
    assert all(a["decision"] == "deny" and a["action"] == "api.authenticate" for a in audits)


def test_downstream_policy_denial_passes_through(client, make_token, auth):
    r = _run(client, auth(make_token(["auditor"])), name="forbidden-agent")
    assert r.status_code == 403


def test_run_is_audited_for_allowed_caller(client, make_token, auth, capsys):
    _run(client, auth(make_token(["student"], sub="user:stu-9")))
    audits = [json.loads(l) for l in capsys.readouterr().out.splitlines() if '"event": "audit"' in l]
    assert any(a["decision"] == "allow" and a["principal"] == "user:stu-9"
               and a["action"] == "api.agents.run:nexa-tutor" for a in audits)


@pytest.mark.parametrize("body", [
    {"messages": []},
    {"messages": [{"role": "system", "content": "x"}]},
    {"messages": [{"role": "user", "content": ""}]},
    {"messages": [{"role": "user", "content": "x"}], "temperature": 9},
])
def test_request_validation(client, make_token, auth, body):
    assert _run(client, auth(make_token(["teacher"])), body=body).status_code == 422


def test_agent_name_is_validated(client, make_token, auth):
    r = _run(client, auth(make_token(["teacher"])), name="Bad_Name")
    assert r.status_code == 422


def test_runtime_unreachable_is_502(monkeypatch, make_token, auth):
    def down(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(api, "_transport", httpx.MockTransport(down))
    with TestClient(api.app) as c:
        r = _run(c, auth(make_token(["teacher"])))
    assert r.status_code == 502
    assert r.json()["error"]["status"] == 502


def test_openapi_is_generated_from_code():
    spec = api.app.openapi()
    assert "/v1/agents" in spec["paths"]
    assert "/v1/agents/{name}/run" in spec["paths"]
    schemas = spec["components"]["schemas"]
    assert {"RunRequest", "RunResponse", "ErrorResponse", "AgentList"} <= set(schemas)
    assert "/metrics" not in spec["paths"]


def test_metrics_endpoint_counts_requests(client):
    client.get("/health")
    body = client.get("/metrics").text
    assert 'nexa_http_requests_total{method="GET",route="/health",service="nexa-api-gateway",status="200"}' in body
