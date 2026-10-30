"""NEXA shared observability (M1). See NEXA-ARCH-001 section 15 and NEXA-M1-007.

One JSON log format and one trace id scheme for every v1 service. Logs are one
JSON object per line to stdout, captured by journald, so all services share a
single sink. Sensitive and student data is redacted (NEXA-GOV-001 section 12).
"""
from .core import (  # noqa: F401
    TRACE_HEADER,
    Timer,
    TraceMiddleware,
    audit_event,
    log_event,
    metrics,
    metrics_endpoint,
    new_trace_id,
    redact,
    span,
)

__all__ = [
    "TRACE_HEADER",
    "Timer",
    "TraceMiddleware",
    "audit_event",
    "log_event",
    "metrics",
    "metrics_endpoint",
    "new_trace_id",
    "redact",
    "span",
]
__version__ = "0.1.0"
