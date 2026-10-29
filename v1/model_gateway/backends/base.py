"""Backend interface and shared retry policy.

Every runtime (Ollama now, vLLM or NVIDIA NIM later) implements this same small
interface, so adding a runtime does not change the gateway or its callers.
See NEXA-ADR-0001.
"""
from __future__ import annotations
import asyncio
import os
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

# Retry and timeout policy, shared by all backends. Transient failures (network
# errors and upstream 5xx) are retried with exponential backoff; 4xx is not.
RETRY_ATTEMPTS = int(os.getenv("NEXA_GATEWAY_RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF = float(os.getenv("NEXA_GATEWAY_RETRY_BACKOFF", "0.5"))

_T = TypeVar("_T")


async def retry_async(fn: Callable[[], Awaitable[_T]]) -> _T:
    last: Optional[Exception] = None
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            return await fn()
        except httpx.HTTPStatusError as exc:
            # Do not retry client errors; they will not succeed on a repeat.
            if exc.response.status_code < 500:
                raise
            last = exc
        except httpx.RequestError as exc:
            last = exc
        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
    assert last is not None
    raise last


class Backend:
    runtime: str = "base"

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: Optional[int],
    ) -> dict:
        """Return {"content": str, "usage": dict, "raw": dict}."""
        raise NotImplementedError

    async def embeddings(self, model: str, inputs: list[str]) -> list[list[float]]:
        """Return one vector per input, in order."""
        raise NotImplementedError
