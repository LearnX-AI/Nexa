"""Trace id, structured logging, redaction, audit events and request middleware.

Every service logs one JSON object per line to stdout. Every request carries a
trace id: honoured from the inbound X-NEXA-Trace-Id header when present, else
minted, and set on the response. The proxy hops forward the header, so one id
links the whole chain. Sensitive and student data is redacted before logging.
"""
from __future__ import annotations
import json
import sys
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

TRACE_HEADER = "X-NEXA-Trace-Id"

# Keys whose values must never appear in logs. Covers secrets and student or
# user content (NEXA-GOV-001 section 12, NEXA-M0-004 privacy).
SENSITIVE_KEYS = {
    "authorization", "token", "access_token", "password", "secret", "api_key",
    "openai_api_key", "email", "user_email", "name", "user_name", "prompt",
    "message", "messages", "content", "query", "text", "input", "output",
    "image_base64",
}

_REDACTED = "[redacted]"


def new_trace_id() -> str:
    return uuid.uuid4().hex


def redact(obj: Any) -> Any:
    """Return a copy with sensitive keys masked, recursively."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if k.lower() in SENSITIVE_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def log_event(**fields) -> None:
    fields.setdefault("ts", round(time.time(), 3))
    fields.setdefault("severity", "info")
    sys.stdout.write(json.dumps(redact(fields), default=str) + "\n")
    sys.stdout.flush()


def audit_event(action: str, decision: str, principal: str, **extra) -> None:
    """Record a privileged action or policy decision. decision is allow|deny."""
    log_event(
        event="audit",
        severity="notice",
        action=action,
        decision=decision,
        principal=principal,
        **extra,
    )


class Timer:
    """Measures wall time in milliseconds for one call."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.ms = round((time.perf_counter() - self._start) * 1000, 1)


class TraceMiddleware(BaseHTTPMiddleware):
    """Assigns a trace id, logs one structured record per request."""

    def __init__(self, app, service: str):
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
        request.state.trace_id = trace_id
        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers[TRACE_HEADER] = trace_id
        log_event(
            event="request",
            service=self.service,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            latency_ms=latency_ms,
            trace_id=trace_id,
        )
        return response
