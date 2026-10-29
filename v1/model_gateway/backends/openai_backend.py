"""OpenAI backend over the OpenAI HTTP API.

Used for the general knowledge model. The key comes from the environment and is
never logged (NEXA-GOV-001). See NEXA-ADR-0001.
"""
from __future__ import annotations
from typing import Optional
import httpx

from .base import Backend
from .. import config


class OpenAIBackend(Backend):
    runtime = "openai"

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key or config.OPENAI_API_KEY
        self.timeout = timeout or config.REQUEST_TIMEOUT

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def chat(self, model, messages, temperature, max_tokens):
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not configured for the gateway")
        payload = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        return {"content": content, "usage": data.get("usage", {}), "raw": data}

    async def embeddings(self, model, inputs):
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not configured for the gateway")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": model, "input": inputs},
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data.get("data", [])]
