"""Ollama backend over its native HTTP API.

Uses httpx directly so the gateway carries no heavy LLM framework. Serves both
chat and embeddings on the local Ollama server. See NEXA-ADR-0001.
"""
from __future__ import annotations
from typing import Optional
import httpx

from .base import Backend
from .. import config


class OllamaBackend(Backend):
    runtime = "ollama"

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[float] = None):
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.timeout = timeout or config.REQUEST_TIMEOUT

    async def chat(self, model, messages, temperature, max_tokens):
        options = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = (data.get("message") or {}).get("content", "")
        usage = {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
        }
        return {"content": content, "usage": usage, "raw": data}

    async def embeddings(self, model, inputs):
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for text in inputs:
                resp = await client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": model, "prompt": text},
                )
                resp.raise_for_status()
                vectors.append(resp.json().get("embedding", []))
        return vectors
