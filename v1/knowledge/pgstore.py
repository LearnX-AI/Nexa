"""pgvector store (NEXA-ADR-0002, NEXA-M2-004).

Layout in schema NEXA_KNOWLEDGE_PG_SCHEMA (default knowledge):
- index_versions   one row per index version (record as jsonb, status)
- index_alias      the single 'active' pointer, switched in one transaction
- embedding_cache  (fingerprint, text hash) -> vector, so unchanged text is
                   never re-embedded by a rebuild
- vec_<index_id>   one table per index version: a fixed-dimension vector column
                   with an HNSW cosine index (pgvector needs a fixed dimension
                   to index), the chunk text and metadata, and typed filter
                   columns for access control. Retiring an index drops it.

The pgvector extension must already exist in the database (CREATE EXTENSION
needs elevated rights; see migrations/0002_vector_index.sql). The service role
needs CREATE on the schema to add per-index tables.

pgvector 0.6 applies WHERE filters after the HNSW scan, so filtered queries
raise hnsw.ef_search to keep enough candidates.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import config
from .store import IndexRecord, StoreError, VectorStore

_ID_RE = re.compile(r"^[a-z0-9_]{3,48}$")
_RECORD_FIELDS = set(IndexRecord.__dataclass_fields__)


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in v) + "]"


def _parse_vec(s: Any) -> list[float]:
    if isinstance(s, list):
        return [float(x) for x in s]
    return [float(x) for x in str(s).strip("[]").split(",") if x]


class PgVectorStore(VectorStore):
    name = "pgvector"

    def __init__(self, dsn: str, schema: str = "knowledge") -> None:
        if not dsn:
            raise StoreError("NEXA_KNOWLEDGE_PG_DSN is required for the pgvector store")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise StoreError("invalid schema name")
        self.dsn = dsn
        self.schema = schema
        self._pool = None

    # ---------------------------------------------------------------- plumbing

    async def open(self) -> None:
        if self._pool is not None:
            return
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=config.PG_POOL_MAX, open=False,
                                         kwargs={"autocommit": True})
        await self._pool.open(wait=True, timeout=30)
        async with self._pool.connection() as conn:
            ext = await (await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).fetchone()
            if not ext:
                raise StoreError("the pgvector extension is not installed in this database "
                                 "(run migrations/0002_vector_index.sql as an administrator)")
            s = self.schema
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.index_versions (
                    index_id   text PRIMARY KEY,
                    status     text NOT NULL,
                    record     jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now())""")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.index_alias (
                    name     text PRIMARY KEY,
                    index_id text NOT NULL REFERENCES {s}.index_versions (index_id))""")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.embedding_cache (
                    fingerprint text NOT NULL,
                    text_key    text NOT NULL,
                    embedding   real[] NOT NULL,
                    created_at  timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (fingerprint, text_key))""")
        self.pgvector_version = ext[0]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _conn(self):
        if self._pool is None:
            raise StoreError("store is not open")
        return self._pool.connection()

    def _table(self, index_id: str) -> str:
        if not _ID_RE.match(index_id):
            raise StoreError(f"invalid index id {index_id!r}")
        return f"{self.schema}.vec_{index_id}"

    @staticmethod
    def _rec(row) -> IndexRecord:
        data = row if isinstance(row, dict) else json.loads(row)
        return IndexRecord(**{k: v for k, v in data.items() if k in _RECORD_FIELDS})

    # ---------------------------------------------------------------- indexes

    async def create_index(self, rec: IndexRecord) -> IndexRecord:
        t = self._table(rec.index_id)
        if rec.dim < 1 or rec.dim > 16000:
            raise StoreError(f"unsupported dimension {rec.dim}")
        async with self._conn() as conn, conn.transaction():
            await conn.execute(f"INSERT INTO {self.schema}.index_versions (index_id, status, record) "
                               f"VALUES (%s, %s, %s)", (rec.index_id, rec.status, json.dumps(rec.to_dict())))
            await conn.execute(f"""
                CREATE TABLE {t} (
                    chunk_id              text PRIMARY KEY,
                    version_id            text NOT NULL,
                    text                  text NOT NULL,
                    metadata              jsonb NOT NULL,
                    approval_status       text,
                    access_classification text,
                    scope                 text,
                    effective_from        date,
                    effective_to          date,
                    embedding             vector({int(rec.dim)}) NOT NULL)""")
            await conn.execute(f"CREATE INDEX ON {t} (version_id)")
            if rec.dim <= 2000:  # pgvector HNSW limit for vector
                await conn.execute(
                    f"CREATE INDEX ON {t} USING hnsw (embedding vector_cosine_ops) "
                    f"WITH (m = {int(config.HNSW_M)}, ef_construction = {int(config.HNSW_EF_CONSTRUCTION)})")
        return rec

    async def list_indexes(self) -> list[IndexRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT record FROM {self.schema}.index_versions ORDER BY created_at, index_id")).fetchall()
        return [self._rec(r[0]) for r in rows]

    async def get_index(self, index_id: str) -> Optional[IndexRecord]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT record FROM {self.schema}.index_versions WHERE index_id = %s", (index_id,))).fetchone()
        return self._rec(row[0]) if row else None

    async def update_index(self, index_id: str, **fields: Any) -> IndexRecord:
        rec = await self.get_index(index_id)
        if rec is None:
            raise StoreError(f"unknown index {index_id}")
        for k, v in fields.items():
            setattr(rec, k, v)
        async with self._conn() as conn:
            await conn.execute(f"UPDATE {self.schema}.index_versions SET status = %s, record = %s "
                               f"WHERE index_id = %s", (rec.status, json.dumps(rec.to_dict()), index_id))
        return rec

    async def active_index_id(self) -> Optional[str]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT index_id FROM {self.schema}.index_alias WHERE name = 'active'")).fetchone()
        return row[0] if row else None

    async def set_active(self, index_id: str) -> IndexRecord:
        s = self.schema
        async with self._conn() as conn, conn.transaction():
            # Serialise switches; the alias row is the lock.
            await conn.execute(f"LOCK TABLE {s}.index_alias IN EXCLUSIVE MODE")
            row = await (await conn.execute(f"SELECT index_id FROM {s}.index_alias WHERE name = 'active'")).fetchone()
            prev = row[0] if row else None
            rec_row = await (await conn.execute(f"SELECT record FROM {s}.index_versions WHERE index_id = %s",
                                                (index_id,))).fetchone()
            if not rec_row:
                raise StoreError(f"unknown index {index_id}")
            rec = self._rec(rec_row[0])
            if prev and prev != index_id:
                prow = await (await conn.execute(f"SELECT record FROM {s}.index_versions WHERE index_id = %s",
                                                 (prev,))).fetchone()
                if prow:
                    p = self._rec(prow[0])
                    p.status = "ready"
                    await conn.execute(f"UPDATE {s}.index_versions SET status = 'ready', record = %s "
                                       f"WHERE index_id = %s", (json.dumps(p.to_dict()), prev))
                rec.previous_index_id = prev
            from .store import _now
            rec.status, rec.activated_at = "active", _now()
            await conn.execute(f"UPDATE {s}.index_versions SET status = 'active', record = %s WHERE index_id = %s",
                               (json.dumps(rec.to_dict()), index_id))
            await conn.execute(f"INSERT INTO {s}.index_alias (name, index_id) VALUES ('active', %s) "
                               f"ON CONFLICT (name) DO UPDATE SET index_id = EXCLUDED.index_id", (index_id,))
        return rec

    async def drop_index(self, index_id: str) -> None:
        if index_id == await self.active_index_id():
            raise StoreError("cannot drop the active index")
        t = self._table(index_id)
        async with self._conn() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {t}")
        if await self.get_index(index_id):
            await self.update_index(index_id, status="retired")

    # ---------------------------------------------------------------- vectors

    async def upsert(self, index_id: str, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        rec = await self.get_index(index_id)
        if rec is None:
            raise StoreError(f"unknown index {index_id}")
        rows = []
        for it in items:
            if len(it["vector"]) != rec.dim:
                raise StoreError(f"vector dimension {len(it['vector'])} does not match index dimension {rec.dim}")
            md = it.get("metadata") or {}
            rows.append((it["chunk_id"], it["version_id"], it["text"], json.dumps(md, default=str),
                         md.get("approval_status"), md.get("access_classification"), md.get("scope"),
                         md.get("effective_from"), md.get("effective_to"), _vec(it["vector"])))
        t = self._table(index_id)
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(
                f"INSERT INTO {t} (chunk_id, version_id, text, metadata, approval_status, access_classification, "
                f"scope, effective_from, effective_to, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) "
                f"ON CONFLICT (chunk_id) DO UPDATE SET version_id = EXCLUDED.version_id, text = EXCLUDED.text, "
                f"metadata = EXCLUDED.metadata, approval_status = EXCLUDED.approval_status, "
                f"access_classification = EXCLUDED.access_classification, scope = EXCLUDED.scope, "
                f"effective_from = EXCLUDED.effective_from, effective_to = EXCLUDED.effective_to, "
                f"embedding = EXCLUDED.embedding", rows)
        return len(rows)

    async def _live_tables(self, index_id: Optional[str]) -> list[str]:
        ids = [index_id] if index_id else [r.index_id for r in await self.list_indexes() if r.status != "retired"]
        return [self._table(i) for i in ids]

    async def delete_version(self, version_id: str, index_id: Optional[str] = None) -> int:
        n = 0
        async with self._conn() as conn:
            for t in await self._live_tables(index_id):
                cur = await conn.execute(f"DELETE FROM {t} WHERE version_id = %s", (version_id,))
                n += cur.rowcount or 0
        return n

    async def delete_chunks(self, index_id: str, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        async with self._conn() as conn:
            cur = await conn.execute(f"DELETE FROM {self._table(index_id)} WHERE chunk_id = ANY(%s)", (chunk_ids,))
            return cur.rowcount or 0

    async def query(self, index_id: str, vector: list[float], top_k: int, where=None) -> list[dict[str, Any]]:
        t = self._table(index_id)
        clause, params = "TRUE", []
        if where is not None:
            if not hasattr(where, "sql"):
                raise StoreError("the pgvector store needs a RetrievalFilter, not an arbitrary callable")
            clause, params = where.sql()
        v = _vec(vector)
        async with self._conn() as conn, conn.transaction():
            ef = max(config.HNSW_EF_SEARCH, top_k * 4)
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef)}")
            # ORDER BY must be the distance alone: any extra sort key stops the
            # planner from using the HNSW index. Ties are broken below instead.
            rows = await (await conn.execute(
                f"SELECT chunk_id, text, metadata, 1 - (embedding <=> %s::vector) AS score FROM {t} "
                f"WHERE {clause} ORDER BY embedding <=> %s::vector LIMIT %s",
                [v, *params, v, top_k])).fetchall()
        out = [{"chunk_id": r[0], "text": r[1], "metadata": r[2], "score": float(r[3])} for r in rows]
        out.sort(key=lambda r: (-round(r["score"], 9), r["chunk_id"]))
        return out

    async def explain(self, index_id: str, vector: list[float], top_k: int, where=None) -> str:
        """The plan for a query, so operations can confirm the HNSW index is used."""
        t = self._table(index_id)
        clause, params = where.sql() if where is not None else ("TRUE", [])
        v = _vec(vector)
        async with self._conn() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(max(config.HNSW_EF_SEARCH, top_k * 4))}")
            rows = await (await conn.execute(
                f"EXPLAIN SELECT chunk_id FROM {t} WHERE {clause} ORDER BY embedding <=> %s::vector LIMIT %s",
                [*params, v, top_k])).fetchall()
        return "\n".join(r[0] for r in rows)

    async def chunk_ids(self, index_id: str) -> set[str]:
        async with self._conn() as conn:
            rows = await (await conn.execute(f"SELECT chunk_id FROM {self._table(index_id)}")).fetchall()
        return {r[0] for r in rows}

    async def get_vectors(self, index_id: str, chunk_ids: list[str]) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT chunk_id, embedding::text FROM {self._table(index_id)} WHERE chunk_id = ANY(%s)",
                (chunk_ids,))).fetchall()
        return {r[0]: _parse_vec(r[1]) for r in rows}

    # ---------------------------------------------------------------- cache

    async def get_many(self, fingerprint: str, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT text_key, embedding FROM {self.schema}.embedding_cache "
                f"WHERE fingerprint = %s AND text_key = ANY(%s)", (fingerprint, keys))).fetchall()
        return {r[0]: [float(x) for x in r[1]] for r in rows}

    async def put_many(self, fingerprint: str, items: dict[str, list[float]]) -> None:
        if not items:
            return
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.executemany(
                f"INSERT INTO {self.schema}.embedding_cache (fingerprint, text_key, embedding) VALUES (%s, %s, %s) "
                f"ON CONFLICT DO NOTHING", [(fingerprint, k, v) for k, v in items.items()])
