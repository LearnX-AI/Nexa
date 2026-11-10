"""Ollama embedding calls from the Model Gateway (found in the M2-004 DGX run)."""
import asyncio
import json

import httpx
import pytest

from v1.model_gateway.backends import base
from v1.model_gateway.backends.ollama_backend import OllamaBackend


def run(c):
    return asyncio.run(c)


def test_batched_embed_with_truncation():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.url.path, body))
        return httpx.Response(200, json={"embeddings": [[float(len(t)), 1.0] for t in body["input"]]})

    b = OllamaBackend("http://ollama", transport=httpx.MockTransport(handler))
    b.EMBED_BATCH = 3
    out = run(b.embeddings("bge-m3", [f"text {i}" for i in range(7)]))
    assert len(out) == 7 and out[0] == [6.0, 1.0]
    assert [p for p, _ in seen] == ["/api/embed"] * 3          # 7 texts in batches of 3
    assert all(body["truncate"] is True for _, body in seen)


def test_falls_back_to_legacy_endpoint_on_old_ollama():
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path == "/api/embed":
            return httpx.Response(404, text="404 page not found")
        return httpx.Response(200, json={"embedding": [0.5, 0.5]})

    b = OllamaBackend("http://ollama", transport=httpx.MockTransport(handler))
    assert run(b.embeddings("nomic-embed-text", ["a", "b"])) == [[0.5, 0.5], [0.5, 0.5]]
    assert paths == ["/api/embed", "/api/embeddings", "/api/embeddings"]


def test_count_mismatch_is_an_error():
    b = OllamaBackend("http://ollama", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"embeddings": [[1.0]]})))
    with pytest.raises(ValueError, match="1 embeddings for 2 inputs"):
        run(b.embeddings("m", ["a", "b"]))


def test_client_error_does_not_open_the_circuit(monkeypatch):
    """A rejected request (4xx) is the caller's problem, not a runtime outage."""
    base.reset_breakers()
    b = OllamaBackend("http://ollama", transport=httpx.MockTransport(
        lambda req: httpx.Response(400, json={"error": "bad input"})))
    for _ in range(base.BREAKER_THRESHOLD + 2):
        with pytest.raises(httpx.HTTPStatusError):
            run(base.guarded("ollama", lambda: b.embeddings("m", ["x"])))
    assert base.breaker_for("ollama").state == "closed"
    base.reset_breakers()
