"""Durable store of the agent registry (NEXA-M3-002).

SQLite (standard library): one file per registry, `NEXA_AGENT_REGISTRY_DB`
(default ~/.nexa/agent-registry.sqlite; ":memory:" in tests). One connection
behind a lock, so the async service can use it from any request.

Tables
  agent_versions  every registered version of every agent: the manifest as
                  approved (JSON), its fingerprint, lifecycle state, who
                  registered it and when. A version is immutable: the same
                  agent and version can only be registered again with the
                  same fingerprint.
  agents          one row per agent: whether it is enabled (the kill switch,
                  separate from the lifecycle) and why not.
  capabilities    agent, version, capability: what discovery searches.
  deployments     where a version runs: location (core:dgx, school:pomnsoe),
                  endpoint, runtime instance, last heartbeat, health.
  events          append-only audit trail of every change: who, what, when.
  workflow_runs   orchestration runs (NEXA-M3-003): status, initiator, context.
  workflow_trace  append-only trace of every run: each step, decision, call.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  disabled_reason TEXT, disabled_by TEXT, disabled_at REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_versions (
  agent_id TEXT NOT NULL REFERENCES agents(agent_id),
  version TEXT NOT NULL,
  major INTEGER NOT NULL, minor INTEGER NOT NULL, patch INTEGER NOT NULL,
  state TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  manifest TEXT NOT NULL,
  registered_by TEXT NOT NULL, registered_at REAL NOT NULL,
  state_changed_by TEXT, state_changed_at REAL,
  PRIMARY KEY (agent_id, version)
);
CREATE TABLE IF NOT EXISTS capabilities (
  agent_id TEXT NOT NULL, version TEXT NOT NULL, capability TEXT NOT NULL,
  PRIMARY KEY (agent_id, version, capability)
);
CREATE INDEX IF NOT EXISTS capabilities_by_name ON capabilities (capability);
CREATE TABLE IF NOT EXISTS deployments (
  deployment_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL, version TEXT NOT NULL,
  location TEXT NOT NULL, endpoint TEXT NOT NULL, instance TEXT NOT NULL,
  registered_by TEXT NOT NULL, registered_at REAL NOT NULL,
  last_heartbeat REAL NOT NULL, health TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS deployments_by_agent ON deployments (agent_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  agent_id TEXT, version TEXT, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_by_agent ON events (agent_id, id);
-- Orchestration (NEXA-M3-003): one row per workflow run, and its trace.
CREATE TABLE IF NOT EXISTS workflow_runs (
  run_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL, workflow_version TEXT NOT NULL, workflow_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  initiator TEXT NOT NULL, initiator_roles TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  input TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}',
  current_step TEXT, output TEXT, error TEXT,
  counters TEXT NOT NULL DEFAULT '{}',
  waiting_until REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_runs_by_status ON workflow_runs (status, workflow_id);
CREATE TABLE IF NOT EXISTS workflow_trace (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  ts REAL NOT NULL, step TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (run_id, seq)
);
"""


def default_path() -> str:
    return os.environ.get("NEXA_AGENT_REGISTRY_DB") or os.path.expanduser("~/.nexa/agent-registry.sqlite")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    # ------------------------------------------------------------ plumbing

    def tx(self):
        """A transaction: every change and its audit event commit together."""
        store = self

        class _Tx:
            def __enter__(self):
                store._lock.acquire()
                store._db.execute("BEGIN IMMEDIATE")
                return store._db

            def __exit__(self, exc_type, *_):
                try:
                    store._db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    store._lock.release()
        return _Tx()

    def query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    @staticmethod
    def event(db, actor: str, action: str, agent_id: Optional[str] = None, version: Optional[str] = None,
              **detail: Any) -> None:
        db.execute("INSERT INTO events (ts, actor, action, agent_id, version, detail) VALUES (?,?,?,?,?,?)",
                   (time.time(), actor, action, agent_id, version, json.dumps(detail, sort_keys=True, default=str)))

    def close(self) -> None:
        with self._lock:
            self._db.close()
