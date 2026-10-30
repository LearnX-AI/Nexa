"""Ollama backend over its native HTTP API.

Uses httpx directly so the gateway carries no heavy LLM framework. Serves both
chat and embeddings on the local Ollama server. Transient failures are retried
by the shared policy in base.py. See NEXA-ADR-0001.
"""
from __future__ import annotations
import json
from typing import Optional
import httpx

from .base import Backend, retry_async
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

        async def _call():
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.base_url}/api/chat", json=payload)
                resp.raise_for_status()
                return resp.json()

        data = await retry_async(_call)
        content = (data.get("message") or {}).get("content", "")
        usage = {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
        }
        return {"content": content, "usage": usage, "raw": data}

    async def chat_stream(self, model, messages, temperature, max_tokens):
        options = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload = {"model": model, "messages": messages, "stream": True,
                   "options": options}
        usage: dict = {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    delta = (obj.get("message") or {}).get("content", "")
                    if delta:
                        yield {"delta": delta, "usage": {}, "done": False}
                    if obj.get("done"):
                        usage = {
                            "prompt_tokens": obj.get("prompt_eval_count", 0),
                            "completion_tokens": obj.get("eval_count", 0),
                        }
                        break
        yield {"delta": "", "usage": usage, "done": True}

    async def embeddings(self, model, inputs):
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for text in inputs:
                async def _call(text=text):
                    resp = await client.post(
                        f"{self.base_url}/api/embeddings",
                        json={"model": model, "prompt": text},
                    )
                    resp.raise_for_status()
                    return resp.json()

                data = await retry_async(_call)
                vectors.append(data.get("embedding", []))
        return vectors
