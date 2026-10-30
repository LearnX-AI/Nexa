"""Model Gateway tests (NEXA-M1-003): adapter switching, streaming, circuit
breaker, registry config, telemetry. No network: backends are fakes."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.model_gateway import main as gw, registry
from v1.model_gateway.backends import base
from v1.model_gateway.backends.base import Backend, CircuitBreaker
from v1.model_gateway.backends.nim_backend import NIMBackend
from v1.model_gateway.backends.openai_backend import OpenAIBackend
from v1.model_gateway.backends.vllm_backend import VLLMBackend
from v1.observability import metrics


class FakeBackend(Backend):
    def __init__(self, runtime, text, fail=False):
        self.runtime = runtime
        self.text = text
        self.fail = fail
        self.calls = 0

    async def chat(self, model, messages, temperature, max_tokens):
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("boom")
        return {"content": self.text,
                "usage": {"prompt_tokens": 3, "completion_tokens": 5}}

    async def chat_stream(self, model, messages, temperature, max_tokens):
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("boom")
        yield {"delta": "Hel", "usage": {}, "done": False}
        yield {"delta": "lo", "usage": {}, "done": False}
        yield {"delta": "", "usage": {"prompt_tokens": 2, "completion_tokens": 2}, "done": True}


@pytest.fixture
def fakes(monkeypatch):
    backends = {
        "ollama": FakeBackend("ollama", "from-ollama"),
        "openai": FakeBackend("openai", "from-openai"),
        "vllm": FakeBackend("vllm", "from-vllm"),
        "nim": FakeBackend("nim", "from-nim"),
    }
    monkeypatch.setattr(gw, "_BACKENDS", backends)
    return backends


@pytest.fixture
def client(fakes):
    with TestClient(gw.app) as c:
        yield c


def _chat(client, headers, model="nexa-chat-default", **extra):
    return client.post(
        "/v1/chat/completions", headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra},
    )


def test_two_adapters_serve_two_logical_names(client, make_token, auth):
    h = auth(make_token(["teacher"]))
    assert _chat(client, h, "nexa-chat-default").json()["choices"][0]["message"]["content"] == "from-ollama"
    assert _chat(client, h, "nexa-general").json()["choices"][0]["message"]["content"] == "from-openai"


def test_switch_runtime_by_config_only(client, make_token, auth, tmp_path):
    """The ADR-0001 acceptance: same call, different adapter, no code change."""
    h = auth(make_token(["teacher"]))
    assert _chat(client, h).json()["x_nexa"]["runtime"] == "ollama"

    cfg = tmp_path / "models.json"
    cfg.write_text(json.dumps({"models": [{
        "logical": "nexa-chat-default", "runtime": "vllm",
        "backend_model": "meta-llama/Llama-3.1-8B-Instruct",
        "version": "vllm-1", "capability": "chat"}]}))
    registry.reload(str(cfg))

    body = _chat(client, h).json()
    assert body["x_nexa"]["runtime"] == "vllm"
    assert body["x_nexa"]["model_version"] == "vllm-1"
    assert body["choices"][0]["message"]["content"] == "from-vllm"


def test_registry_rejects_bad_config(tmp_path):
    bad_runtime = tmp_path / "a.json"
    bad_runtime.write_text(json.dumps({"models": [{
        "logical": "x", "runtime": "nope", "backend_model": "m",
        "version": "1", "capability": "chat"}]}))
    with pytest.raises(registry.RegistryError):
        registry.reload(str(bad_runtime))
    with pytest.raises(registry.RegistryError):
        registry.reload(str(tmp_path / "missing.json"))
    empty = tmp_path / "b.json"
    empty.write_text(json.dumps({"models": []}))
    with pytest.raises(registry.RegistryError):
        registry.reload(str(empty))


def test_registry_aliases_and_version_pin(tmp_path):
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({"models": [{
        "logical": "nexa-chat-default", "runtime": "ollama", "backend_model": "m",
        "version": "v7", "capability": "chat", "aliases": ["nexa-chat"]}]}))
    registry.reload(str(cfg))
    assert registry.resolve("nexa-chat").logical == "nexa-chat-default"
    assert registry.resolve("nexa-chat-default@v7") is not None
    assert registry.resolve("nexa-chat-default@v8") is None


def test_example_config_is_valid():
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "model_gateway" / "models.example.json"
    registry.reload(str(path))
    assert registry.resolve("nexa-chat-vllm").runtime == "vllm"
    assert registry.resolve("nexa-chat-nim").runtime == "nim"


def test_gateway_refuses_to_start_on_bad_model_config(monkeypatch, tmp_path, fakes):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(gw.config, "MODEL_CONFIG", str(bad))
    with pytest.raises(registry.RegistryError):
        with TestClient(gw.app):
            pass


def test_streaming_returns_sse_chunks(client, make_token, auth):
    h = auth(make_token(["teacher"]))
    r = _chat(client, h, stream=True)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-nexa-trace-id"]
    events = [e[len("data: "):] for e in r.text.strip().split("\n\n")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["completion_tokens"] == 2


def test_circuit_opens_after_repeated_upstream_failures(client, make_token, auth, fakes, monkeypatch):
    monkeypatch.setattr(base, "BREAKER_THRESHOLD", 2)
    base.reset_breakers()
    fakes["ollama"].fail = True
    h = auth(make_token(["teacher"]))
    assert _chat(client, h).status_code == 502
    assert _chat(client, h).status_code == 502
    calls = fakes["ollama"].calls
    r = _chat(client, h)
    assert r.status_code == 503           # fails fast, no upstream call
    assert fakes["ollama"].calls == calls
    assert _chat(client, h, "nexa-general").status_code == 200   # other runtime unaffected
    assert _chat(client, h, stream=True).status_code == 503


def test_circuit_breaker_half_open_recovery():
    now = [0.0]
    br = CircuitBreaker(failure_threshold=2, reset_after=10, clock=lambda: now[0])
    br.record_failure()
    br.record_failure()
    assert br.state == "open" and not br.allow()
    now[0] = 11
    assert br.state == "half_open" and br.allow()
    br.record_success()
    assert br.state == "closed"


def test_token_and_latency_telemetry(client, make_token, auth):
    h = auth(make_token(["teacher"]))
    _chat(client, h)
    out = metrics.render()
    assert 'nexa_model_requests_total{model="nexa-chat-default",outcome="ok",runtime="ollama"} 1' in out
    assert 'nexa_model_tokens_total{kind="completion",model="nexa-chat-default"} 5' in out
    assert "nexa_model_latency_ms_count" in out


def test_unauthorized_roles_and_unknown_model(client, make_token, auth):
    assert _chat(client, auth(make_token(["auditor"]))).status_code == 403
    assert _chat(client, {}).status_code == 401
    assert _chat(client, auth(make_token(["teacher"])), "nope").status_code == 404


def test_new_adapters_use_openai_compatible_shape():
    for cls, runtime in ((VLLMBackend, "vllm"), (NIMBackend, "nim"), (OpenAIBackend, "openai")):
        b = cls(base_url="http://x/v1", api_key="k")
        assert b.runtime == runtime
        assert b._headers() == {"Authorization": "Bearer k"}
    # A self-hosted vLLM needs no key; OpenAI does.
    assert VLLMBackend(base_url="http://x/v1", api_key="")._headers() == {}
    with pytest.raises(RuntimeError):
        OpenAIBackend(base_url="http://x/v1", api_key="")._check()


@pytest.mark.parametrize("cls", [VLLMBackend, NIMBackend])
def test_openai_compat_chat_and_stream_against_mock_server(cls, monkeypatch):
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            sse = (
                'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"!"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=sse)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Hi!"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "v1.model_gateway.backends.openai_compat.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    backend = cls(base_url="http://runtime/v1")

    async def run():
        chat = await backend.chat("m", [{"role": "user", "content": "x"}], 0.2, None)
        items = [i async for i in backend.chat_stream("m", [{"role": "user", "content": "x"}], 0.2, None)]
        return chat, items

    chat, items = asyncio.run(run())
    assert chat["content"] == "Hi!"
    assert "".join(i["delta"] for i in items) == "Hi!"
    assert items[-1]["done"] and items[-1]["usage"]["completion_tokens"] == 2
