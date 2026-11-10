"""Source, run, job and chunk registry (NEXA-M2-002).

Idempotency rules:
- A source version is identified by its content checksum. The same bytes are
  stored once, whatever file name or source_id they arrive under.
- A job is identified by an idempotency key over (checksum, pipeline version,
  normalizer version, chunker version, chunk config hash, manifest hash).
  Submitting the same thing twice returns the existing job, creates nothing.
- Reprocessing the same bytes with a new pipeline or chunker version creates a
  new run and a new chunk set; the previous chunk set is deactivated, never
  duplicated alongside it.
- A new checksum for an existing source_id is a new version. When it becomes
  active the older versions are marked superseded and their chunks deactivated.

Backends: SQLite (DEV, single-host QA, tests via :memory:). The PostgreSQL
schema for ADR-0002 is in migrations/0001_knowledge_registry.sql with the same
tables; its adapter lands with the pgvector retrieval step.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import uuid
from typing import Any, Iterable, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS sources (
    version_id      TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    checksum        TEXT NOT NULL UNIQUE,
    metadata_json   TEXT NOT NULL,
    status          TEXT NOT NULL,              -- active | superseded | withdrawn
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sources_source_id ON sources(source_id);

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    version_id          TEXT NOT NULL,
    pipeline_version    TEXT NOT NULL,
    normalizer_version  TEXT NOT NULL,
    chunker_version     TEXT NOT NULL,
    chunk_config_hash   TEXT NOT NULL,
    extractor           TEXT NOT NULL,
    extractor_version   TEXT NOT NULL,
    ocr_json            TEXT,
    stats_json          TEXT NOT NULL,
    quality_json        TEXT NOT NULL,
    status              TEXT NOT NULL,          -- succeeded | partial
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_version ON runs(version_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    checksum        TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    mime_type       TEXT,
    version_id      TEXT,
    run_id          TEXT,
    state           TEXT NOT NULL,   -- queued | running | succeeded | partial | failed | quarantined | duplicate
    stage           TEXT NOT NULL,   -- validate | extract | scan | metadata | chunk | index | done
    attempts        INTEGER NOT NULL DEFAULT 0,
    retryable       INTEGER NOT NULL DEFAULT 0,
    error_code      TEXT,
    error_detail    TEXT,
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    manifest_json   TEXT NOT NULL DEFAULT '{}',
    principal       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS ix_jobs_checksum ON jobs(checksum);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    active          INTEGER NOT NULL,
    embedded        INTEGER NOT NULL DEFAULT 0,
    text            TEXT NOT NULL,
    embedding_text  TEXT NOT NULL,
    record_json     TEXT NOT NULL,
    UNIQUE (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_chunks_version_active ON chunks(version_id, active);

-- NEXA-M2-007 LMS synchronisation. One row per LMS item, and one per sync
-- event received (the event log is the audit trail and the retry queue).
CREATE TABLE IF NOT EXISTS lms_items (
    item_key        TEXT PRIMARY KEY,           -- system:site_id:item_id
    system          TEXT NOT NULL,
    site_id         TEXT NOT NULL,
    course_id       TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    state           TEXT NOT NULL,              -- active | pending | withdrawn | rejected | failed
    revision        TEXT,
    checksum        TEXT,
    version_id      TEXT,
    last_event_id   TEXT,
    last_event_at   TEXT,                       -- occurred_at of the newest applied event
    detail          TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lms_items_course ON lms_items(system, site_id, course_id);

CREATE TABLE IF NOT EXISTS lms_events (
    event_id        TEXT PRIMARY KEY,
    item_key        TEXT NOT NULL,
    op              TEXT NOT NULL,              -- upsert | delete
    occurred_at     TEXT NOT NULL,
    state           TEXT NOT NULL,              -- queued | applied | failed | rejected | stale
    attempts        INTEGER NOT NULL DEFAULT 0,
    retryable       INTEGER NOT NULL DEFAULT 0,
    error_code      TEXT,
    error_detail    TEXT,
    job_id          TEXT,
    version_id      TEXT,
    payload_json    TEXT NOT NULL,
    principal       TEXT,
    received_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lms_events_state ON lms_events(state);
CREATE INDEX IF NOT EXISTS ix_lms_events_item ON lms_events(item_key, occurred_at);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Registry:
    def __init__(self, url: str = "memory") -> None:
        if url in ("memory", "sqlite://", "sqlite:///:memory:"):
            path = ":memory:"
        elif url.startswith("sqlite:///"):
            path = url[len("sqlite:///"):]
        else:
            raise ValueError(f"unsupported registry url {url!r} (use memory or sqlite:///path)")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_DDL)

    # ---- low level ----

    def _one(self, sql: str, args: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(args)).fetchall()]

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self._lock:
            self._db.execute(sql, tuple(args))

    # ---- jobs ----

    def find_job_by_key(self, key: str) -> Optional[dict[str, Any]]:
        # Same shape as get_job (decoded warnings and manifest, boolean
        # retryable): callers cannot tell a new job from an existing one.
        row = self._one("SELECT job_id FROM jobs WHERE idempotency_key=?", (key,))
        return self.get_job(row["job_id"]) if row else None

    def create_job(self, key: str, checksum: str, file_name: str, mime: Optional[str],
                   manifest: dict[str, Any], principal: Optional[str]) -> dict[str, Any]:
        job_id = "job_" + uuid.uuid4().hex[:20]
        now = _now()
        self._exec(
            "INSERT INTO jobs (job_id, idempotency_key, checksum, file_name, mime_type, state, stage, "
            "manifest_json, principal, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, key, checksum, file_name, mime, "queued", "validate", json.dumps(manifest, default=str),
             principal, now, now))
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        j = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if j:
            j["warnings"] = json.loads(j.pop("warnings_json") or "[]")
            j["manifest"] = json.loads(j.pop("manifest_json") or "{}")
            j["retryable"] = bool(j["retryable"])
        return j

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        if "warnings" in fields:
            fields["warnings_json"] = json.dumps(fields.pop("warnings"))
        if "manifest" in fields:
            fields["manifest_json"] = json.dumps(fields.pop("manifest"), default=str)
        if "retryable" in fields:
            fields["retryable"] = int(bool(fields["retryable"]))
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE job_id=?", (*fields.values(), job_id))
        return self.get_job(job_id)  # type: ignore[return-value]

    def list_jobs(self, state: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        if state:
            rows = self._all("SELECT job_id FROM jobs WHERE state=? ORDER BY created_at DESC LIMIT ?", (state, limit))
        else:
            rows = self._all("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.get_job(r["job_id"]) for r in rows]  # type: ignore[misc]

    # ---- sources ----

    def get_source_by_checksum(self, checksum: str) -> Optional[dict[str, Any]]:
        s = self._one("SELECT * FROM sources WHERE checksum=?", (checksum,))
        if s:
            s["metadata"] = json.loads(s.pop("metadata_json"))
        return s

    def get_source(self, version_id: str) -> Optional[dict[str, Any]]:
        s = self._one("SELECT * FROM sources WHERE version_id=?", (version_id,))
        if s:
            s["metadata"] = json.loads(s.pop("metadata_json"))
        return s

    def versions_of(self, source_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM sources WHERE source_id=? ORDER BY created_at", (source_id,))
        for r in rows:
            r["metadata"] = json.loads(r.pop("metadata_json"))
        return rows

    def upsert_source(self, version_id: str, source_id: str, checksum: str, metadata: dict[str, Any]) -> None:
        now = _now()
        with self._lock:
            existing = self._one("SELECT version_id FROM sources WHERE version_id=?", (version_id,))
            if existing:
                self._exec("UPDATE sources SET metadata_json=?, updated_at=? WHERE version_id=?",
                           (json.dumps(metadata, default=str), now, version_id))
            else:
                self._exec("INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                           (version_id, source_id, checksum, json.dumps(metadata, default=str), "active", now, now))

    def supersede_older(self, source_id: str, keep_version_id: str) -> list[str]:
        """Mark other active versions of source_id superseded and deactivate their chunks."""
        with self._lock:
            olds = self._all("SELECT version_id FROM sources WHERE source_id=? AND version_id<>? AND status='active'",
                             (source_id, keep_version_id))
            ids = [o["version_id"] for o in olds]
            for vid in ids:
                self._exec("UPDATE sources SET status='superseded', updated_at=? WHERE version_id=?", (_now(), vid))
                self._exec("UPDATE chunks SET active=0 WHERE version_id=?", (vid,))
        return ids

    def withdraw_source(self, source_id: str) -> list[str]:
        """Mark every version of a source withdrawn and deactivate its chunks.
        Nothing is deleted: the versions stay in the registry as history."""
        with self._lock:
            rows = self._all("SELECT version_id FROM sources WHERE source_id=? AND status<>'withdrawn'", (source_id,))
            ids = [r["version_id"] for r in rows]
            for vid in ids:
                self._exec("UPDATE sources SET status='withdrawn', updated_at=? WHERE version_id=?", (_now(), vid))
                self._exec("UPDATE chunks SET active=0 WHERE version_id=?", (vid,))
        return ids

    def reactivate_source(self, version_id: str) -> None:
        """A withdrawn version whose item came back unchanged: make it active
        again, with its chunk set from its latest run."""
        with self._lock:
            src = self._one("SELECT status FROM sources WHERE version_id=?", (version_id,))
            if not src or src["status"] != "withdrawn":
                return
            run = self._one("SELECT run_id FROM runs WHERE version_id=? ORDER BY created_at DESC LIMIT 1",
                            (version_id,))
            self._exec("UPDATE sources SET status='active', updated_at=? WHERE version_id=?", (_now(), version_id))
            if run:
                self._exec("UPDATE chunks SET active=1 WHERE version_id=? AND run_id=?", (version_id, run["run_id"]))

    def list_sources(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources" + (" WHERE status=?" if status else "") + " ORDER BY source_id, created_at"
        rows = self._all(sql, (status,) if status else ())
        for r in rows:
            r["metadata"] = json.loads(r.pop("metadata_json"))
        return rows

    # ---- runs and chunks ----

    def save_run(self, run: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
        """Persist a run and make its chunk set the active one for that version,
        in one transaction."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run["run_id"], run["version_id"], run["pipeline_version"], run["normalizer_version"],
                     run["chunker_version"], run["chunk_config_hash"], run["extractor"], run["extractor_version"],
                     json.dumps(run.get("ocr")), json.dumps(run["stats"]), json.dumps(run["quality"]),
                     run["status"], _now()))
                self._db.execute("UPDATE chunks SET active=0 WHERE version_id=?", (run["version_id"],))
                for c in chunks:
                    self._db.execute(
                        "INSERT INTO chunks (chunk_id, version_id, run_id, ordinal, active, text, embedding_text, "
                        "record_json) VALUES (?,?,?,?,1,?,?,?) "
                        "ON CONFLICT(chunk_id) DO UPDATE SET active=1, run_id=excluded.run_id, "
                        "text=excluded.text, embedding_text=excluded.embedding_text, "
                        "record_json=excluded.record_json, embedded=0",
                        (c["chunk_id"], run["version_id"], run["run_id"], c["ordinal"], c["text"],
                         c["embedding_text"], json.dumps(c, default=str)))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        r = self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if r:
            for k in ("ocr_json", "stats_json", "quality_json"):
                r[k[:-5]] = json.loads(r.pop(k) or "null")
        return r

    def runs_of(self, version_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT run_id FROM runs WHERE version_id=? ORDER BY created_at", (version_id,))
        return [self.get_run(r["run_id"]) for r in rows]  # type: ignore[misc]

    def chunks_of(self, version_id: str, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT record_json, embedded FROM chunks WHERE version_id=?" + (" AND active=1" if active_only else "")
        rows = self._all(sql + " ORDER BY ordinal", (version_id,))
        out = []
        for r in rows:
            rec = json.loads(r["record_json"])
            rec["embedded"] = bool(r["embedded"])
            out.append(rec)
        return out

    def get_chunk(self, chunk_id: str) -> Optional[dict[str, Any]]:
        r = self._one("SELECT record_json, active, version_id, run_id FROM chunks WHERE chunk_id=?", (chunk_id,))
        if not r:
            return None
        rec = json.loads(r["record_json"])
        rec.update(active=bool(r["active"]), version_id=r["version_id"], run_id=r["run_id"])
        return rec

    # ---- LMS synchronisation (NEXA-M2-007) ----

    def get_lms_item(self, item_key: str) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM lms_items WHERE item_key=?", (item_key,))

    def put_lms_item(self, item_key: str, **f: Any) -> dict[str, Any]:
        f["updated_at"] = _now()
        with self._lock:
            if self.get_lms_item(item_key):
                cols = ", ".join(f"{k}=?" for k in f)
                self._exec(f"UPDATE lms_items SET {cols} WHERE item_key=?", (*f.values(), item_key))
            else:
                f["item_key"] = item_key
                cols = ", ".join(f)
                self._exec(f"INSERT INTO lms_items ({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
        return self.get_lms_item(item_key)  # type: ignore[return-value]

    def lms_items_of(self, system: str, site_id: str, course_id: Optional[str] = None) -> list[dict[str, Any]]:
        if course_id is None:
            return self._all("SELECT * FROM lms_items WHERE system=? AND site_id=? ORDER BY item_key",
                             (system, site_id))
        return self._all("SELECT * FROM lms_items WHERE system=? AND site_id=? AND course_id=? ORDER BY item_key",
                         (system, site_id, course_id))

    def get_lms_event(self, event_id: str) -> Optional[dict[str, Any]]:
        e = self._one("SELECT * FROM lms_events WHERE event_id=?", (event_id,))
        if e:
            e["payload"] = json.loads(e.pop("payload_json"))
            e["retryable"] = bool(e["retryable"])
        return e

    def add_lms_event(self, event_id: str, item_key: str, op: str, occurred_at: str, payload: dict[str, Any],
                      principal: Optional[str], state: str = "queued") -> dict[str, Any]:
        now = _now()
        self._exec("INSERT INTO lms_events (event_id, item_key, op, occurred_at, state, payload_json, principal, "
                   "received_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (event_id, item_key, op, occurred_at, state, json.dumps(payload, default=str), principal, now, now))
        return self.get_lms_event(event_id)  # type: ignore[return-value]

    def update_lms_event(self, event_id: str, **f: Any) -> dict[str, Any]:
        if "retryable" in f:
            f["retryable"] = int(bool(f["retryable"]))
        f["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in f)
        self._exec(f"UPDATE lms_events SET {cols} WHERE event_id=?", (*f.values(), event_id))
        return self.get_lms_event(event_id)  # type: ignore[return-value]

    def list_lms_events(self, state: Optional[str] = None, limit: int = 100,
                        item_key: Optional[str] = None) -> list[dict[str, Any]]:
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if item_key:
            where.append("item_key=?")
            args.append(item_key)
        sql = "SELECT event_id FROM lms_events" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._all(sql + " ORDER BY received_at DESC, event_id LIMIT ?", (*args, limit))
        return [self.get_lms_event(r["event_id"]) for r in rows]  # type: ignore[misc]

    def lms_counts(self) -> dict[str, Any]:
        ev = {r["state"]: r["n"] for r in self._all("SELECT state, COUNT(*) n FROM lms_events GROUP BY state")}
        it = {r["state"]: r["n"] for r in self._all("SELECT state, COUNT(*) n FROM lms_items GROUP BY state")}
        oldest = self._one("SELECT MIN(received_at) t FROM lms_events WHERE state IN ('queued', 'failed')")
        last = self._one("SELECT MAX(updated_at) t FROM lms_events WHERE state='applied'")
        return {"events": ev, "items": it, "oldest_unapplied_event": oldest["t"] if oldest else None,
                "last_applied_at": last["t"] if last else None}

    def unembedded_chunks(self, version_id: str) -> list[dict[str, Any]]:
        return [c for c in self.chunks_of(version_id) if not c["embedded"]]

    def mark_embedded(self, chunk_ids: list[str]) -> None:
        with self._lock:
            for cid in chunk_ids:
                self._db.execute("UPDATE chunks SET embedded=1 WHERE chunk_id=?", (cid,))

    def counts(self) -> dict[str, int]:
        return {
            "sources": self._one("SELECT COUNT(*) n FROM sources")["n"],  # type: ignore[index]
            "active_sources": self._one("SELECT COUNT(*) n FROM sources WHERE status='active'")["n"],  # type: ignore[index]
            "runs": self._one("SELECT COUNT(*) n FROM runs")["n"],  # type: ignore[index]
            "jobs": self._one("SELECT COUNT(*) n FROM jobs")["n"],  # type: ignore[index]
            "chunks": self._one("SELECT COUNT(*) n FROM chunks")["n"],  # type: ignore[index]
            "active_chunks": self._one("SELECT COUNT(*) n FROM chunks WHERE active=1")["n"],  # type: ignore[index]
        }
