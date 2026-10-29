"""vLLM backend adapter (stub).

vLLM exposes an OpenAI compatible HTTP API, so this adapter will subclass the
same Backend interface and reuse the OpenAI request shape against the vLLM
server URL. It is a stub until a vLLM runtime is approved and available
(NEXA-ADR-0001, NEXA-M1-003). Registering it is a config change in main.py, with
no change to callers.
"""
from __future__ import annotations

from .base import Backend


class VLLMBackend(Backend):
    runtime = "vllm"

    async def chat(self, model, messages, temperature, max_tokens):
        raise NotImplementedError(
            "vLLM adapter not enabled. Approve and configure a vLLM server, then "
            "implement against its OpenAI compatible API."
        )

    async def embeddings(self, model, inputs):
        raise NotImplementedError("vLLM embeddings not enabled.")
