"""OpenAI backend over the OpenAI HTTP API.

Used for the general knowledge model. The key comes from the environment and is
never logged (NEXA-GOV-001). Transient failures are retried by the shared policy
in base.py. See NEXA-ADR-0001.
"""
from __future__ import annotations
from typing import Optional

from .openai_compat import OpenAICompatBackend
from .. import config


class OpenAIBackend(OpenAICompatBackend):
    runtime = "openai"
    require_key = True
    key_env = "OPENAI_API_KEY"

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: Optional[float] = None):
        super().__init__(
            base_url or config.OPENAI_BASE_URL,
            api_key if api_key is not None else config.OPENAI_API_KEY,
            timeout,
        )
