"""A fair queue in front of answer generation.

Retrieval costs milliseconds; writing an answer with a local LLM costs tens of
seconds and the model serves only a few at a time (Ollama's
OLLAMA_NUM_PARALLEL). Without a limit, a classroom of simultaneous questions
all reach the model at once, wait inside the gateway, and most hit its timeout.
With NEXA_GENERATION_CONCURRENCY = N, at most N generations run at once and the
rest wait their turn in arrival order (asyncio.Semaphore is FIFO), each for at
most NEXA_GENERATION_QUEUE_TIMEOUT_S. A question that cannot get a turn in time
gets the controlled 'generation_unavailable' answer, with its passages, instead
of an error. 0 = no limit (the default on core NEXA, whose gateway scales).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from . import config


class QueueTimeout(Exception):
    pass


class Limiter:
    def __init__(self) -> None:
        self._sem: Optional[asyncio.Semaphore] = None
        self._size = 0
        self.waiting = 0
        self.running = 0
        self.max_waiting = 0
        self.completed = 0
        self.timed_out = 0
        self.total_wait_s = 0.0
        self.max_wait_s = 0.0

    def _semaphore(self) -> Optional[asyncio.Semaphore]:
        n = config.GENERATION_CONCURRENCY
        if n <= 0:
            return None
        if self._sem is None or self._size != n:
            self._sem, self._size = asyncio.Semaphore(n), n
        return self._sem

    async def run(self, fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        sem = self._semaphore()
        if sem is None:
            return await fn(*args)
        t0 = time.monotonic()
        self.waiting += 1
        self.max_waiting = max(self.max_waiting, self.waiting)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=config.GENERATION_QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.waiting -= 1
            self.timed_out += 1
            raise QueueTimeout(f"waited {config.GENERATION_QUEUE_TIMEOUT_S:.0f}s for a free answer slot")
        waited = time.monotonic() - t0
        self.waiting -= 1
        self.running += 1
        self.total_wait_s += waited
        self.max_wait_s = max(self.max_wait_s, waited)
        try:
            return await fn(*args)
        finally:
            self.running -= 1
            self.completed += 1
            sem.release()

    def stats(self) -> dict[str, Any]:
        return {"concurrency": config.GENERATION_CONCURRENCY, "running": self.running, "waiting": self.waiting,
                "max_waiting": self.max_waiting, "completed": self.completed, "timed_out": self.timed_out,
                "mean_wait_s": round(self.total_wait_s / self.completed, 2) if self.completed else 0.0,
                "max_wait_s": round(self.max_wait_s, 2)}


GENERATION = Limiter()
