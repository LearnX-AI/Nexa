"""NEXA Model Gateway configuration.

All values come from the environment. No secrets in source (NEXA-GOV-001,
NEXA-ADR-0005). See NEXA-ADR-0001 for the gateway decision.
"""
from __future__ import annotations
import os

# Upstream runtimes the gateway fronts in M1.
OLLAMA_BASE_URL = os.getenv("NEXA_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OPENAI_BASE_URL = os.getenv("NEXA_OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# vLLM and NVIDIA NIM expose OpenAI-compatible APIs (NEXA-M1-003). Defaults are
# local placeholders; real URLs and any keys come from the environment file.
VLLM_BASE_URL = os.getenv("NEXA_VLLM_BASE_URL", "http://127.0.0.1:8200/v1")
VLLM_API_KEY = os.getenv("NEXA_VLLM_API_KEY", "")
NIM_BASE_URL = os.getenv("NEXA_NIM_BASE_URL", "http://127.0.0.1:8300/v1")
NIM_API_KEY = os.getenv("NEXA_NIM_API_KEY", "")

# Optional JSON file that defines the model registry (logical names, runtime,
# backend model, version, aliases). When unset the built-in defaults in
# registry.py are used. Per-model configuration lives outside application code.
MODEL_CONFIG = os.getenv("NEXA_MODEL_CONFIG", "")

# Gateway auth. Shared bearer token so model endpoints are never open to end
# users. This is a placeholder until centralized identity and RBAC land in
# ADR-0005.
GATEWAY_TOKEN = os.getenv("NEXA_GATEWAY_TOKEN", "")

# Upstream request timeout in seconds.
REQUEST_TIMEOUT = float(os.getenv("NEXA_GATEWAY_TIMEOUT", "60"))
