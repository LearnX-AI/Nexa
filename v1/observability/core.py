"""Trace id, structured logging, redaction, audit events and request middleware.

Every service logs one JSON object per line to stdout. Every request carries a
trace id: honoured from the inbound X-NEXA-Trace-Id header when present, else
minted, and set on the response. The proxy hops forward the header, so one id
links the whole chain. Sensitive and student data is redacted before logging.
"""
from __future__ import annotations
import json
import sys
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

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


class Metrics:
    """Tiny in-process metrics registry rendered in Prometheus text format.

    Counters and sum/count pairs only. No client library, no external service.
    Labels must be low cardinality (route templates, model names, status codes),
    never user ids, emails or prompts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple, float] = defaultdict(float)

    @staticmethod
    def _key(name: str, labels: dict) -> tuple:
        return (name, tuple(sorted((k, str(v)) for k, v in labels.items())))

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        with self._lock:
            self._values[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels) -> None:
        """Record one observation as <name>_count and <name>_sum."""
        with self._lock:
            self._values[self._key(name + "_count", labels)] += 1
            self._values[self._key(name + "_sum", labels)] += value

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> str:
        lines = []
        with self._lock:
            items = sorted(self._values.items())
        for (name, labels), value in items:
            if labels:
                inner = ",".join(
                    '{}="{}"'.format(k, v.replace("\\", "/").replace('"', "'"))
                    for k, v in labels
                )
                lines.append(f"{name}{{{inner}}} {value:g}")
            else:
                lines.append(f"{name} {value:g}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


async def metrics_endpoint(request: Request) -> PlainTextResponse:
    """GET /metrics. Services bind to 127.0.0.1; scrape from the host only."""
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@contextmanager
def span(name: str, trace_id: Optional[str] = None, **fields) -> Iterator[None]:
    """Trace hook for a RAG step or tool call (NEXA-M1-007).

    Logs one structured record with latency and status, under the request's
    trace id, and records a latency metric. Field values pass through redact().
    """
    start = time.perf_counter()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        ms = round((time.perf_counter() - start) * 1000, 1)
        log_event(event="span", span=name, status=status, latency_ms=ms,
                  trace_id=trace_id, **fields)
        metrics.observe("nexa_span_ms", ms, span=name, status=status)


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
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.inc("nexa_http_requests_total", service=self.service,
                    method=request.method, route=route, status=response.status_code)
        metrics.observe("nexa_http_request_ms", latency_ms,
                        service=self.service, route=route)
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
