"""Model Gateway observability.

Re-exports the shared observability layer (NEXA-ARCH-001 section 15, M1-007) so
the whole platform uses one JSON log format and one trace id scheme. Existing
imports in this package stay unchanged.
"""
from v1.observability import new_trace_id, log_event, Timer  # noqa: F401

__all__ = ["new_trace_id", "log_event", "Timer"]
