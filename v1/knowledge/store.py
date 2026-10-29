"""Vector store interface and the M1 in-memory backend.

The store is pluggable so the pgvector backend (NEXA-ADR-0002) drops in later
without changing the service. The in-memory backend is for the M1 skeleton and
for tests; it does not persist and is not shared across workers.
"""
from __future__ import annotations
import math
from typing import Any

from . import config


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class VectorStore:
    """Store interface. Backends implement add and query."""

    async def add(self, items: list[dict[str, Any]]) -> int:
        raise NotImplementedError

    async def query(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        raise NotImplementedError


class InMemoryStore(VectorStore):
    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []

    async def add(self, items: list[dict[str, Any]]) -> int:
        self._items.extend(items)
        return len(items)

    async def query(self, vector: list[float], top_k: int) -> list[dict[str, Any]]:
        scored = [
            {
                "text": it["text"],
                "metadata": it.get("metadata", {}),
                "score": _cosine(vector, it["vector"]),
            }
            for it in self._items
        ]
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]


def get_store() -> VectorStore:
    backend = config.STORE_BACKEND
    if backend == "memory":
        return InMemoryStore()
    if backend == "pgvector":
        # Deferred to the ADR-0002 implementation step. Do not build here
        # (NEXA-DEV-001 section 15).
        raise NotImplementedError(
            "pgvector backend is not implemented in the M1 skeleton"
        )
    raise ValueError(f"Unknown store backend: {backend}")
