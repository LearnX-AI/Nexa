"""Backend interface.

Every runtime (Ollama now, vLLM or NVIDIA NIM later) implements this same small
interface, so adding a runtime does not change the gateway or its callers.
See NEXA-ADR-0001.
"""
from __future__ import annotations
from typing import Optional


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
