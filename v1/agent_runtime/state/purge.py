"""Retention of short-lived state (NEXA-M3-006).

  sessions          deleted when they expire (idle ttl or maximum age)
  run state         for runs that ended more than NEXA_STATE_RETENTION_DAYS
                    ago (default 30): checkpoints deleted, the run's input,
                    working context and output cleared; the status, counters,
                    trace and message log stay as the audit record
  idempotency keys  of those runs, deleted

Runs at start and then every NEXA_STATE_PURGE_S seconds (default 3600) inside
the Agent Runtime; also `python -m v1.tools.state_retention`. Each purge is
logged in state_access as the actor system:state-retention.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from v1.identity.principal import Principal

from . import store as state

SYSTEM = Principal("system:state-retention", ("service",), "service")
TERMINAL = ("succeeded", "rejected", "failed", "budget_exceeded", "expired")


def retention_days() -> float:
    return float(os.environ.get("NEXA_STATE_RETENTION_DAYS", "30"))


def purge(now: Optional[float] = None, days: Optional[float] = None, dry_run: bool = False) -> dict[str, Any]:
    now = now or time.time()
    days = retention_days() if days is None else days
    st = state._store()
    expired = [r["session_id"] for r in st.query("SELECT session_id FROM sessions WHERE expires_at < ? OR "
                                                 "max_expires_at < ?", (now, now))]
    cutoff = now - days * 86400
    old = [r["run_id"] for r in st.query(
        f"SELECT run_id FROM workflow_runs WHERE status IN ({','.join('?' * len(TERMINAL))}) AND updated_at < ? "
        "AND input != '{\"purged\": true}'", (*TERMINAL, cutoff))]
    if not dry_run:
        for sid in expired:
            state.delete_session_row(sid)
            state.log_access(SYSTEM, "purge", "session", sid, reason="expired")
        for rid in old:
            with st.tx() as db:
                db.execute("DELETE FROM run_checkpoints WHERE run_id = ?", (rid,))
                db.execute("UPDATE workflow_runs SET input = '{\"purged\": true}', context = '{\"steps\": {}, "
                           "\"purged\": true}', output = NULL WHERE run_id = ?", (rid,))
                db.execute("DELETE FROM message_idempotency WHERE idempotency_key LIKE ?", (f"{rid}/%",))
            state.log_access(SYSTEM, "purge", "run", rid, reason=f"ended more than {days:g} days ago")
    return {"sessions_expired": len(expired), "runs_purged": len(old), "retention_days": days, "dry_run": dry_run}
