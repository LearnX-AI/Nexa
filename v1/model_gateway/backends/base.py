"""Backend interface, shared retry policy and circuit breaker.

Every runtime (Ollama, OpenAI, vLLM, NVIDIA NIM) implements this same small
interface, so adding a runtime does not change the gateway or its callers.
See NEXA-ADR-0001.

Resilience policy (NEXA-M1-003), shared by all backends:
- Timeout: set per backend from NEXA_GATEWAY_TIMEOUT.
- Retry: transient failures (network errors, upstream 5xx) are retried with
  exponential backoff; 4xx is not.
- Circuit breaker: after NEXA_GATEWAY_BREAKER_THRESHOLD consecutive upstream
  failures a runtime is marked open and calls fail fast for
  NEXA_GATEWAY_BREAKER_RESET seconds, then one trial call is allowed.
"""
from __future__ import annotations
import asyncio
import os
import time
from typing import AsyncIterator, Awaitable, Callable, Optional, TypeVar

import httpx

RETRY_ATTEMPTS = int(os.getenv("NEXA_GATEWAY_RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF = float(os.getenv("NEXA_GATEWAY_RETRY_BACKOFF", "0.5"))
BREAKER_THRESHOLD = int(os.getenv("NEXA_GATEWAY_BREAKER_THRESHOLD", "5"))
BREAKER_RESET = float(os.getenv("NEXA_GATEWAY_BREAKER_RESET", "30"))

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


class CircuitOpenError(RuntimeError):
    """Raised instead of calling a runtime whose circuit is open."""


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_after: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        self.failures = 0
        self.opened_at: Optional[float] = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self._clock() - self.opened_at >= self.reset_after:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self._clock()


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker_for(runtime: str) -> CircuitBreaker:
    br = _BREAKERS.get(runtime)
    if br is None:
        br = _BREAKERS[runtime] = CircuitBreaker(BREAKER_THRESHOLD, BREAKER_RESET)
    return br


def reset_breakers() -> None:
    _BREAKERS.clear()


def is_upstream_failure(exc: Exception) -> bool:
    """True for runtime outages. Bad requests and local config errors do not
    count, so a typo in a model name cannot open the circuit."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError)


async def guarded(runtime: str, fn: Callable[[], Awaitable[_T]]) -> _T:
    """Run fn under the runtime's circuit breaker."""
    br = breaker_for(runtime)
    if not br.allow():
        raise CircuitOpenError(runtime)
    try:
        result = await fn()
    except Exception as exc:
        if is_upstream_failure(exc):
            br.record_failure()
        raise
    br.record_success()
    return result


async def guarded_stream(runtime: str, stream: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Stream under the circuit breaker. The outcome is recorded when the
    stream ends or fails; a stream is never retried once it has started."""
    br = breaker_for(runtime)
    try:
        async for item in stream:
            yield item
    except Exception as exc:
        if is_upstream_failure(exc):
            br.record_failure()
        raise
    br.record_success()


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

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: Optional[int],
    ) -> AsyncIterator[dict]:
        """Yield {"delta": str, "usage": dict, "done": bool} items.

        Default: one non-streamed call returned as a single chunk, so a runtime
        without native streaming still satisfies the contract.
        """
        result = await self.chat(model, messages, temperature, max_tokens)
        yield {"delta": result["content"], "usage": result.get("usage", {}), "done": True}

    async def embeddings(self, model: str, inputs: list[str]) -> list[list[float]]:
        """Return one vector per input, in order."""
        raise NotImplementedError
