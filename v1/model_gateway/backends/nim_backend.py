"""NVIDIA NIM backend adapter.

NIM microservices expose an OpenAI-compatible API, so this adapter points the
shared implementation at the NIM endpoint. Use it only where NIM is approved and
available (NEXA-ADR-0001, NEXA-M1-003). A self-hosted NIM needs no key; the
hosted NVIDIA API needs NEXA_NIM_API_KEY, which comes from the environment file
and is never committed.
"""
from __future__ import annotations
from typing import Optional

from .openai_compat import OpenAICompatBackend
from .. import config


class NIMBackend(OpenAICompatBackend):
    runtime = "nim"
    key_env = "NEXA_NIM_API_KEY"

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        super().__init__(
            base_url or config.NIM_BASE_URL,
            api_key if api_key is not None else config.NIM_API_KEY,
            timeout,
        )
