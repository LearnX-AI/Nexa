"""Embedding client for the Knowledge Service.

All embedding traffic goes through the Model Gateway (NEXA-ADR-0001), never a
direct embedding SDK. The service presents its own service token and forwards a
trace id so the gateway logs under the same id.
"""
from __future__ import annotations
from typing import Optional
import httpx

from . import config


class EmbeddingError(Exception):
    pass


async def embed(texts: list[str], trace_id: Optional[str] = None) -> list[list[float]]:
    if not config.GATEWAY_TOKEN:
        raise EmbeddingError("NEXA_KNOWLEDGE_GATEWAY_TOKEN not set")
    if not texts:
        return []
    headers = {"Authorization": f"Bearer {config.GATEWAY_TOKEN}"}
    if trace_id:
        headers["X-NEXA-Trace-Id"] = trace_id
    payload = {"model": config.EMBED_MODEL, "input": texts}
    url = f"{config.GATEWAY_URL}/v1/embeddings"
    try:
        async with httpx.AsyncClient(timeout=config.GATEWAY_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise EmbeddingError(f"gateway unreachable: {exc}")
    if resp.status_code != 200:
        raise EmbeddingError(f"gateway {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        return [row["embedding"] for row in data["data"]]
    except (KeyError, TypeError):
        raise EmbeddingError("unexpected embeddings response shape")
