"""vLLM backend adapter.

vLLM serves an OpenAI-compatible API (`vllm serve <model>`), so this adapter only
points the shared OpenAI-compatible implementation at the vLLM server URL.
Enabling a vLLM model is a registry entry with runtime "vllm" (see
models.example.json), with no change to callers (NEXA-ADR-0001, NEXA-M1-003).
The API key is optional: set NEXA_VLLM_API_KEY only if the server was started
with --api-key.
"""
from __future__ import annotations
from typing import Optional

from .openai_compat import OpenAICompatBackend
from .. import config


class VLLMBackend(OpenAICompatBackend):
    runtime = "vllm"
    key_env = "NEXA_VLLM_API_KEY"

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        super().__init__(
            base_url or config.VLLM_BASE_URL,
            api_key if api_key is not None else config.VLLM_API_KEY,
            timeout,
        )
