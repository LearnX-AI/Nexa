"""Audit trail of the Tool Gateway (NEXA-M3-004).

One append-only record per call, allowed or refused, in SQLite
(NEXA_TOOL_AUDIT_DB, default ~/.nexa/tool-gateway.sqlite; ":memory:" in
tests), and the same record in the structured audit log.

Each record: when, call id, trace id, workflow run and step; the user and
roles; the agent and version; the tool, version, implementation and connector;
the scopes used; a digest of the arguments and a preview with personal data
masked; the decision, the check that decided it and why; the outcome status,
latency, output size and output digest. Never a credential, never the full
arguments or output.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from v1.observability import audit_event

from .policy import PII

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, call_id TEXT NOT NULL, trace_id TEXT,
  run_id TEXT, step TEXT,
  user TEXT, roles TEXT, agent TEXT, agent_version TEXT,
  tool TEXT, tool_version TEXT, kind TEXT, connector TEXT, scopes TEXT,
  args_digest TEXT, args_preview TEXT,
  decision TEXT NOT NULL, check_name TEXT, reason TEXT,
  status INTEGER, latency_ms INTEGER, output_bytes INTEGER, output_digest TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_by_trace ON tool_calls (trace_id);
CREATE INDEX IF NOT EXISTS tool_calls_by_run ON tool_calls (run_id);
"""

FIELDS = ("ts", "call_id", "trace_id", "run_id", "step", "user", "roles", "agent", "agent_version", "tool",
          "tool_version", "kind", "connector", "scopes", "args_digest", "args_preview", "decision", "check_name",
          "reason", "status", "latency_ms", "output_bytes", "output_digest")


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def preview(value: Any, n: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    for rx in PII.values():
        text = rx.sub("[masked]", text)
    return text if len(text) <= n else text[:n] + "…"


class Audit:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("NEXA_TOOL_AUDIT_DB") or os.path.expanduser("~/.nexa/tool-gateway.sqlite")
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    def record(self, **rec: Any) -> None:
        rec.setdefault("ts", time.time())
        for k in ("roles", "scopes"):
            if isinstance(rec.get(k), (list, tuple)):
                rec[k] = json.dumps(list(rec[k]))
        row = tuple(rec.get(f) for f in FIELDS)
        with self._lock:
            self._db.execute(f"INSERT INTO tool_calls ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
                             row)
        audit_event(action=f"tool.call:{rec.get('agent')}:{rec.get('tool')}", decision=rec["decision"],
                    principal=rec.get("user") or "unknown", call_id=rec.get("call_id"), check=rec.get("check_name"),
                    reason=rec.get("reason"), status=rec.get("status"), run_id=rec.get("run_id"),
                    trace_id=rec.get("trace_id"))

    def query(self, *, trace_id: Optional[str] = None, run_id: Optional[str] = None, tool: Optional[str] = None,
              user: Optional[str] = None, call_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        for col, v in (("trace_id", trace_id), ("run_id", run_id), ("tool", tool), ("user", user),
                       ("call_id", call_id)):
            if v:
                where.append(f"{col} = ?")
                args.append(v)
        sql = "SELECT * FROM tool_calls" + (" WHERE " + " AND ".join(where) if where else "") + \
            " ORDER BY seq DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("roles", "scopes"):
                d[k] = json.loads(d[k]) if d.get(k) else []
            d["check"] = d.pop("check_name")
            out.append(d)
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()
