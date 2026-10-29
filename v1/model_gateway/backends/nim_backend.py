"""NVIDIA NIM backend adapter (stub).

NIM microservices expose an OpenAI compatible HTTP API. This adapter will reuse
the same Backend interface against a NIM endpoint. It is a stub until a NIM
runtime is approved and available (NEXA-ADR-0001, NEXA-M1-003). Registering it
is a config change in main.py, with no change to callers.
"""
from __future__ import annotations

from .base import Backend


class NIMBackend(Backend):
    runtime = "nim"

    async def chat(self, model, messages, temperature, max_tokens):
        raise NotImplementedError(
            "NVIDIA NIM adapter not enabled. Approve and configure a NIM endpoint, "
            "then implement against its OpenAI compatible API."
        )

    async def embeddings(self, model, inputs):
        raise NotImplementedError("NIM embeddings not enabled.")
