"""Shared adapter for runtimes that expose the OpenAI-compatible HTTP API.

OpenAI, vLLM and NVIDIA NIM all speak the same chat, embeddings and server-sent
event streaming shapes, so one implementation serves all three. Each runtime is
a thin subclass that only sets its name, base URL and key (NEXA-ADR-0001,
NEXA-M1-003). Uses httpx directly, no vendor SDK. Keys come from configuration
and are never logged.
"""
from __future__ import annotations
import json
from typing import AsyncIterator, Optional
import httpx

from .base import Backend, retry_async
from .. import config


class OpenAICompatBackend(Backend):
    runtime = "openai-compat"
    require_key = False
    key_env = "API_KEY"

    def __init__(self, base_url: str, api_key: str = "",
                 timeout: Optional[float] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout or config.REQUEST_TIMEOUT

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _check(self) -> None:
        if self.require_key and not self.api_key:
            raise RuntimeError(f"{self.key_env} not configured for the gateway")

    @staticmethod
    def _payload(model, messages, temperature, max_tokens) -> dict:
        payload = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return payload

    async def chat(self, model, messages, temperature, max_tokens):
        self._check()
        payload = self._payload(model, messages, temperature, max_tokens)

        async def _call():
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()

        data = await retry_async(_call)
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        return {"content": content, "usage": data.get("usage", {}), "raw": data}

    async def chat_stream(self, model, messages, temperature, max_tokens) -> AsyncIterator[dict]:
        self._check()
        payload = self._payload(model, messages, temperature, max_tokens)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        usage: dict = {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions",
                headers=self._headers(), json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    delta = (choices[0].get("delta") or {}).get("content") if choices else None
                    if delta:
                        yield {"delta": delta, "usage": {}, "done": False}
        yield {"delta": "", "usage": usage, "done": True}

    async def embeddings(self, model, inputs):
        self._check()

        async def _call():
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    headers=self._headers(),
                    json={"model": model, "input": inputs},
                )
                resp.raise_for_status()
                return resp.json()

        data = await retry_async(_call)
        return [item["embedding"] for item in data.get("data", [])]
